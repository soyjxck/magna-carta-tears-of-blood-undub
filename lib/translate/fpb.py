"""``.fpb`` (PlayBook) — windowed-pool dialog format.

A ``.fpb`` file is one binary "data section" (the underlying text bytes
for a whole scene) plus a record table whose entries are
``(seq_id, offset, length)`` windows into that section.

Records routinely OVERLAP — one record's window can span an entire
multi-line monologue while another's is a short slice of it — so
treating each record as a separate string is misleading. The catalog
therefore exposes the **whole data section** as one editable ``en``
string; on rebuild we diff old↔new via ``difflib.SequenceMatcher`` and
remap each window's ``(offset, length)`` so it still bounds the same
logical text in the new bytes (no overshoot into the next dialog box).

On-disk layout::

    +0x00  u32  count                   (= n_records + 1; last slot is sentinel)
    +0x04  3*u32 zero                   (12 bytes header padding; 16 total)
    +0x10  per record (count-1, 12B)    u32 seq_id, u32 offset, u32 length
    +...   u32 data_section_size        (the "sentinel")
    +...   data_section_size bytes      raw text storage

Earlier TECHNICAL.md notes had the record fields documented as
``(seq, length, offset)`` — that was wrong. The engine reads each
record as ``data[offset : offset + length]``.
"""
from __future__ import annotations

import json
import struct
from difflib import SequenceMatcher
from pathlib import Path

from ._common import (CATALOG_DIR, decode_safe, encode_en, open_ship_handles)

Window = tuple[int, int, int]  # (seq, offset, length)


def synthesize_implicit_seq0(windows: list[Window],
                             data_len: int | None = None) -> list[Window]:
    """Prepend a synthetic ``seq=0`` window covering the bytes before the
    first explicit window (or the whole data section if there are none).

    By convention, every byte of an .fpb's data section belongs to some
    record. When the first explicit window starts past offset 0, the
    bytes before it form an implicit ``seq=0`` record (verified across
    all 652 .fpb files in Magna Carta — none start at offset 0 unless
    they explicitly include a ``seq=0`` entry). When a file has zero
    explicit windows but a non-empty data section (65 / 711 .fpb files),
    the entire data section is the implicit ``seq=0``.

    No-op when:
      - the first window already starts at offset 0, or
      - any window already has ``seq == 0`` (the record is explicit), or
      - windows is empty and ``data_len`` is None or 0.

    The synthesized window is purely a view: the on-disk format does not
    store ``seq=0`` for files using the implicit convention, and
    ``build_fpb`` doesn't write it. Round-trip is preserved.
    """
    if any(seq == 0 for seq, _, _ in windows):
        return list(windows)
    if not windows:
        if data_len:
            return [(0, 0, data_len)]
        return []
    first_offset = windows[0][1]
    if first_offset == 0:
        return list(windows)
    return [(0, 0, first_offset)] + list(windows)


# --------------------------------------------------------------------------- parse / build

def parse_fpb_raw(blob: bytes) -> tuple[bytes, list[Window], bytes]:
    """Return ``(header_16, [(seq, offset, length)...], data_section_bytes)``.

    The data section is the underlying text storage; record windows
    index into it.
    """
    if len(blob) < 20:
        raise ValueError(f".fpb too small ({len(blob)} B)")
    n = struct.unpack_from("<I", blob, 0)[0]
    real = n - 1
    if real < 0:
        raise ValueError(f".fpb count={n} (must be >= 1)")
    header16 = blob[:16]
    sentinel_off = 16 + real * 12
    if sentinel_off + 4 > len(blob):
        raise ValueError(
            f".fpb truncated: sentinel offset {sentinel_off} > {len(blob)}"
        )
    data_size = struct.unpack_from("<I", blob, sentinel_off)[0]
    data = blob[sentinel_off + 4 : sentinel_off + 4 + data_size]
    windows: list[Window] = []
    for i in range(real):
        seq, offset, length = struct.unpack_from("<III", blob, 16 + i * 12)
        windows.append((int(seq), int(offset), int(length)))
    return header16, windows, data


def build_fpb(header16: bytes, windows: list[Window], data_section: bytes) -> bytes:
    """Re-emit ``.fpb`` bytes with the given windows + data section.

    Windows whose ``offset + length`` would overrun the (possibly edited)
    data section are clamped to fit. The header's first u32 ``count`` is
    rewritten to ``len(windows) + 1`` so the engine sees the correct
    record count (matters when the catalog was edited).

    The header field at offset +0x0C is the **implicit seq=0 length** —
    the engine reads bytes ``data[0:implicit_len]`` for record 0 instead
    of using the windows table. Verified across all 652 .fpb files in
    Magna Carta: every file has ``header[+0x0C] u32 LE == first explicit
    window's offset``. We patch this field on rebuild so seq=0 reads the
    correct slice; without the patch the engine reads the original
    USA-sized window from our differently-sized data, bleeding adjacent
    records into the seq=0 dialog box.
    """
    if len(header16) != 16:
        raise ValueError(f"header16 must be 16 bytes, got {len(header16)}")
    n = len(windows) + 1
    table = bytearray()
    data_len = len(data_section)
    for seq, offset, length in windows:
        if offset > data_len:
            offset, length = 0, 0
        elif offset + length > data_len:
            length = data_len - offset
        table += struct.pack("<III", seq, offset, length)

    # Patch the implicit seq=0 length to match the new layout. Equals the
    # first explicit window's offset; 0 if no explicit windows exist.
    implicit_seq0_len = windows[0][1] if windows else 0
    header16 = (header16[:0x0C]
                + struct.pack("<I", implicit_seq0_len)
                + header16[0x10:])

    out = bytearray()
    out += struct.pack("<I", n)
    out += header16[4:]
    out += table
    out += struct.pack("<I", data_len)
    out += data_section
    return bytes(out)


# --------------------------------------------------------------------------- diff-based remap

def remap_windows(old_text: str, old_windows: list[Window],
                  new_text: str) -> list[Window]:
    """Map ``(offset, length)`` windows from old_text → new_text via diff.

    Pool-style editing breaks naively when the translator changes a word
    mid-text: every window past the edit point would otherwise still point
    at its original byte offset and overshoot into the next dialog box.
    Walking the diff:

      - inserts/deletes BEFORE the window: window's offset shifts
      - inserts/deletes INSIDE the window: window's length adjusts
      - inserts/deletes AFTER the window: window unchanged
      - replaces (delete+insert at same point): window edges pin to the
        nearest equal-block boundary in new_text
    """
    sm = SequenceMatcher(None, old_text, new_text, autojunk=False)
    ops = sm.get_opcodes()  # [(tag, i1, i2, j1, j2), ...]

    def map_pos(p: int, is_end: bool) -> int:
        """Map old position ``p`` to new. ``is_end=True`` for the
        half-open interval end (so we round forward through replace ops)."""
        for tag, i1, i2, j1, j2 in ops:
            if i1 <= p < i2:
                if tag == "equal":
                    return j1 + (p - i1)
                if tag == "replace":
                    return j2 if is_end else j1
                if tag == "delete":
                    return j1
                # 'insert' is zero-width in old (i1==i2); won't enter this branch
            if p == i2 and is_end:
                return j2
        return len(new_text)

    out: list[Window] = []
    for seq, offset, length in old_windows:
        new_off = map_pos(offset, is_end=False)
        new_end = map_pos(offset + length, is_end=True)
        out.append((seq, new_off, max(0, new_end - new_off)))
    return out


# --------------------------------------------------------------------------- catalog I/O

def _records_for(blob: bytes, encoding: str) -> tuple[list[Window], bytes] | None:
    """Parse a region's .fpb and synthesize the implicit seq=0 record so the
    returned windows fully partition the data section. Returns None on parse
    failure. The encoding arg is kept for API symmetry with future callers."""
    try:
        _, windows, data = parse_fpb_raw(blob)
    except ValueError:
        return None
    return synthesize_implicit_seq0(windows, data_len=len(data)), data


def extract_all_fpb(usa_ship: Path,
                    kr_ship: Path | None,
                    jp_ship: Path | None,
                    out_dir: Path) -> int:
    """Extract every ``.fpb`` in USA SHIP into per-file JSON catalogs.

    Catalog shape (one record per dialog line, with cross-language refs)::

        {
          "file": "<name>.fpb",
          "records": [
            {"seq": 0, "en": "...", "kr": "...", "jp": "..."},
            {"seq": 1, "en": "...", "kr": "...", "jp": "..."},
            ...
          ]
        }

    KR/JP text is sliced per-record using each region's own ``(offset, length)``
    windows, matched to USA's records by ``seq`` ID. Records that exist in USA
    but not in KR/JP (the 21 / 707 files with localization-branch deltas) get
    no ``kr`` / ``jp`` field on the unmatched record. The implicit ``seq=0``
    prefix is materialized as the first record so every byte of the data
    section is addressable in the catalog.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    usa, usa_idx, kr_pair, jp_pair, fhs = open_ship_handles(
        usa_ship, kr_ship, jp_ship)
    written = 0
    try:
        usa_n = usa.read_filename_toc()
        for i, name in enumerate(usa_n):
            if not name.lower().endswith(".fpb"):
                continue
            usa_blob = usa.read_entry(i, fhs["usa"])
            usa_parsed = _records_for(usa_blob, "latin-1")
            if usa_parsed is None:
                continue
            usa_windows, usa_data = usa_parsed

            kr_by_seq: dict[int, tuple[int, int]] = {}
            kr_data = b""
            if kr_pair is not None and name.lower() in kr_pair[1]:
                blob = kr_pair[0].read_entry(kr_pair[1][name.lower()], fhs["kr"])
                p = _records_for(blob, "cp949")
                if p is not None:
                    kr_w, kr_data = p
                    kr_by_seq = {s: (o, l) for s, o, l in kr_w}

            jp_by_seq: dict[int, tuple[int, int]] = {}
            jp_data = b""
            if jp_pair is not None and name.lower() in jp_pair[1]:
                blob = jp_pair[0].read_entry(jp_pair[1][name.lower()], fhs["jp"])
                p = _records_for(blob, "shift_jis")
                if p is not None:
                    jp_w, jp_data = p
                    jp_by_seq = {s: (o, l) for s, o, l in jp_w}

            records: list[dict] = []
            for seq, off, ln in usa_windows:
                rec: dict = {
                    "seq": seq,
                    "en": decode_safe(usa_data[off:off + ln], "latin-1"),
                }
                if seq in kr_by_seq:
                    ko, kl = kr_by_seq[seq]
                    rec["kr"] = decode_safe(kr_data[ko:ko + kl], "cp949")
                if seq in jp_by_seq:
                    jo, jl = jp_by_seq[seq]
                    rec["jp"] = decode_safe(jp_data[jo:jo + jl], "shift_jis")
                records.append(rec)

            cat = {"file": name, "records": records}
            (out_dir / f"{Path(name).stem}.json").write_text(
                json.dumps(cat, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            written += 1
    finally:
        for fh in fhs.values():
            fh.close()
    return written


def translated_fpb_bytes(name: str, usa_blob: bytes,
                         catalog_dir: Path = CATALOG_DIR / "fpb"
                         ) -> bytes | None:
    """Build ``.fpb`` bytes from a per-record translation catalog. Returns
    None when no catalog exists OR no record was actually edited so the
    caller falls back to raw USA bytes (preserves byte-identical round-trip
    even for the 6 .fpb files that have pool bytes outside any record).

    When edited: encode each record's ``en`` to latin-1, concatenate into a
    fresh data section, and compute windows as ``(seq, running_offset, len)``.
    No diff-remap needed — the catalog already pairs each record with its
    own text. If USA used the implicit-seq=0 convention (every file we've
    seen except 6), the synthetic seq=0 window is dropped from the output
    so the on-disk format matches the original layout.
    """
    cat_path = catalog_dir / f"{Path(name).stem}.json"
    if not cat_path.exists():
        return None
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    cat_records = cat.get("records", [])

    header16, usa_orig_windows, usa_data = parse_fpb_raw(usa_blob)
    usa_synth = synthesize_implicit_seq0(usa_orig_windows, data_len=len(usa_data))

    # Edit detection: any record whose en differs from USA's slice for that
    # window? If nothing changed, return None so the caller writes USA's
    # bytes verbatim — pool data outside any window is preserved.
    if len(cat_records) == len(usa_synth):
        any_edit = False
        for rec, (_, off, ln) in zip(cat_records, usa_synth):
            usa_en = usa_data[off:off + ln].decode("latin-1", errors="replace")
            if rec.get("en", "") != usa_en:
                any_edit = True
                break
        if not any_edit:
            return None

    # At least one record was edited (or catalog count diverges). Rebuild
    # mechanically by concatenating each record's encoded en.
    usa_has_explicit_seq0 = any(s == 0 for s, _, _ in usa_orig_windows)
    data = bytearray()
    windows: list[Window] = []
    for rec in cat_records:
        seq = int(rec["seq"])
        en_bytes = encode_en(rec.get("en", ""))
        offset = len(data)
        windows.append((seq, offset, len(en_bytes)))
        data += en_bytes

    # Drop synthetic seq=0 if USA used the implicit convention.
    if (not usa_has_explicit_seq0 and windows
            and windows[0][0] == 0 and windows[0][1] == 0):
        windows = windows[1:]

    return build_fpb(header16, windows, bytes(data))
