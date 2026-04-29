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
    out = bytearray()
    out += struct.pack("<I", n)
    out += header16[4:]            # 12 zero-padding bytes preserved verbatim
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

def _read_data_section(afs, idx: dict[str, int], name: str, fh) -> bytes | None:
    """Return the data section of `name` from `afs` (decoded as raw bytes),
    or None if the file is missing or malformed."""
    if name.lower() not in idx:
        return None
    blob = afs.read_entry(idx[name.lower()], fh)
    try:
        _, _, data = parse_fpb_raw(blob)
    except ValueError:
        return None
    return data


def extract_all_fpb(usa_ship: Path,
                    kr_ship: Path | None,
                    jp_ship: Path | None,
                    out_dir: Path) -> int:
    """Extract every ``.fpb`` in USA SHIP into per-file JSON catalogs."""
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
            try:
                _, windows, usa_data = parse_fpb_raw(usa_blob)
            except ValueError:
                continue
            cat: dict = {
                "file": name,
                "en": decode_safe(usa_data, "latin-1"),
            }
            if kr_pair is not None:
                kr_data = _read_data_section(kr_pair[0], kr_pair[1], name, fhs["kr"])
                if kr_data is not None:
                    cat["kr"] = decode_safe(kr_data, "cp949")
            if jp_pair is not None:
                jp_data = _read_data_section(jp_pair[0], jp_pair[1], name, fhs["jp"])
                if jp_data is not None:
                    cat["jp"] = decode_safe(jp_data, "shift_jis")
            cat["windows"] = [
                {"seq": s, "offset": o, "length": l} for s, o, l in windows
            ]
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
    """Build ``.fpb`` bytes from a translation catalog. Returns None when
    no catalog exists for ``name`` so the caller falls back to raw USA
    bytes — a half-translated repo still produces a working ISO."""
    cat_path = catalog_dir / f"{Path(name).stem}.json"
    if not cat_path.exists():
        return None
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    header16, orig_windows, orig_data = parse_fpb_raw(usa_blob)
    orig_en = orig_data.decode("latin-1")
    new_en = cat["en"]
    new_data = encode_en(new_en)
    if new_en == orig_en:
        # No edit detected — preserve windows verbatim for a byte-identical rebuild
        windows = orig_windows
    else:
        windows = remap_windows(orig_en, orig_windows, new_en)
    return build_fpb(header16, windows, new_data)
