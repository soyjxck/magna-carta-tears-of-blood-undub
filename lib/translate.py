"""Retranslation pipeline for SHIP.AFS text files.

Per-file JSON catalogs under ``translations/<ext>/<basename>.json``.

Format families
---------------

**1. ``.fpb`` — windowed pool**
Records are ``(seq_id, offset, length)`` windows into a shared data
section. Catalog stores the whole data section as one editable ``en``
string; on rebuild we diff old↔new and remap each window so it still
bounds the same logical text.

**2. Fixed-stride slot files** (``.cht``, ``.odd``, ``.gft``, ``.cha``,
``.cdg``, ``.mdg``, ``.ecd``, ``.fds``)
A short header followed by ``count`` slots of fixed byte stride. Each
slot holds one null-terminated string padded with zeros to the stride.
Per-ext stride is hardcoded in the engine bytecode (verified empirically;
see ``SLOT_FORMATS`` table). Catalog stores per-slot ``en`` strings;
edits up to ``stride - 1`` bytes per slot work without rebuilding any
offset table.

Other text extensions (``.tui``, ``.itm``, ``.abi``, ``.sgi``, ``.nod``,
``.pod``, ``.dod``, ``.cls``, ``.att``, ``.val``) need format-specific
work and aren't covered yet.

Catalog example for ``.fpb``::

    {
      "file": "00001944.fpb",
      "en": "*huff* *huff*...U-Uagh...!There's a village nearby.$nI'm...",
      "kr": "...",
      "jp": "...",
      "windows": [{"seq": 1, "offset": 10, "length": 54}, ...]
    }

Catalog example for fixed-stride slot files::

    {
      "file": "00012047.cht",
      "ext": ".cht",
      "header_hex": "ffffffff...",
      "slot_stride": 532,
      "slots": [
        {"i": 0, "en": "What's the Trinity Circle?", "kr": "...", "jp": "..."},
        ...
      ]
    }

The in-engine line-break token is the literal ASCII sequence ``$n`` —
preserve as-is when editing. USA fonts only render latin-1 characters.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from afs import Afs


CATALOG_DIR = ROOT / "translations"

# Fixed-stride slot formats: header_size + count*slot_stride. Each slot
# holds one null-terminated string. Per-ext (header_size, slot_stride)
# verified empirically against every USA SHIP.AFS file of that ext.
SLOT_FORMATS: dict[str, tuple[int, int]] = {
    ".cht": (28, 532),  # phone conversations / NPC option dialog
    ".odd": (16, 240),  # side-quest text
    ".gft": (16,  74),  # gift dialog
    ".cha": (12, 299),  # character bios
    ".cdg": (16, 263),  # talisman effect descriptions
    ".mdg": (16, 263),  # monster bestiary
    ".ecd": (40, 548),  # event/cutscene dialog
    ".fds": (12, 512),  # friend/team dialog
}

SUPPORTED_EXTS = (".fpb", *SLOT_FORMATS.keys())


# --------------------------------------------------------------------------- .fpb

def parse_fpb_raw(blob: bytes) -> tuple[bytes, list[tuple[int, int, int]], bytes]:
    """Return (header_16, [(seq, offset, length)...], data_section_bytes).

    `data_section_bytes` is the raw underlying text storage; record windows
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
        raise ValueError(f".fpb truncated: sentinel offset {sentinel_off} > {len(blob)}")
    data_size = struct.unpack_from("<I", blob, sentinel_off)[0]
    data = blob[sentinel_off + 4 : sentinel_off + 4 + data_size]
    windows: list[tuple[int, int, int]] = []
    for i in range(real):
        # Record layout: (seq_id, offset, length) — three little-endian u32s.
        # Empirically verified: the engine reads each record as
        # data[offset : offset + length].  An older version of TECHNICAL.md
        # had length and offset swapped — that doc was wrong.
        seq, offset, length = struct.unpack_from("<III", blob, 16 + i * 12)
        windows.append((int(seq), int(offset), int(length)))
    return header16, windows, data


def remap_windows(old_text: str, old_windows: list[tuple[int, int, int]],
                  new_text: str) -> list[tuple[int, int, int]]:
    """Translate window offsets/lengths from old_text → new_text via diff.

    This is what makes pool-style editing work without dialog overshooting
    into the next box. When a translator edits part of the data section,
    every window's `(offset, length)` originally pointed into old_text;
    we diff old↔new and shift each window so it points at the same logical
    region in new_text:

    - inserts/deletes BEFORE the window: window's offset shifts by (+ins -del)
    - inserts/deletes INSIDE the window: window's length adjusts
    - inserts/deletes AFTER the window: window unchanged
    - replaces (delete+insert at same point): window edge pinned to the
      nearest boundary in new_text

    Implementation uses Python's difflib SequenceMatcher opcodes.
    """
    from difflib import SequenceMatcher
    sm = SequenceMatcher(None, old_text, new_text, autojunk=False)
    ops = sm.get_opcodes()  # [(tag, i1, i2, j1, j2), ...]

    def map_pos(p: int, is_end: bool) -> int:
        """Map an old position to new. `is_end=True` for half-open
        interval ends (so we round forward through replace ops)."""
        for tag, i1, i2, j1, j2 in ops:
            if i1 <= p < i2:
                if tag == "equal":
                    return j1 + (p - i1)
                if tag == "replace":
                    return j2 if is_end else j1
                if tag == "delete":
                    return j1
                # 'insert' is zero-width in old (i1==i2); won't enter here
            if p == i2:
                # Boundary between this op and the next; for end-positions
                # we use j2; for start-positions, the next iteration's i1==p
                # will pick it up via 'equal' or it's beyond the last op.
                if is_end:
                    return j2
        return len(new_text)

    out: list[tuple[int, int, int]] = []
    for seq, offset, length in old_windows:
        new_off = map_pos(offset, is_end=False)
        new_end = map_pos(offset + length, is_end=True)
        out.append((seq, new_off, max(0, new_end - new_off)))
    return out


def build_fpb(header16: bytes,
              windows: list[tuple[int, int, int]],
              data_section: bytes) -> bytes:
    """Re-emit .fpb bytes with the given (already remapped) windows + data."""
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
        # Match parse_fpb_raw: (seq, offset, length).
        table += struct.pack("<III", seq, offset, length)
    out = bytearray()
    out += struct.pack("<I", n)
    out += header16[4:]            # 12 zero-padding bytes
    out += table
    out += struct.pack("<I", data_len)
    out += data_section
    return bytes(out)


# --------------------------------------------------------------------------- catalog

def _decode(b: bytes, encoding: str) -> str:
    return b.decode(encoding, errors="replace")


def _encode_en(s: str) -> bytes:
    """English -> bytes via latin-1 (lossless for any 0x00-0xFF input).

    Reject anything outside latin-1 (e.g. emoji) — USA fonts can't render it.
    """
    try:
        return s.encode("latin-1")
    except UnicodeEncodeError as e:
        raise ValueError(
            f"English text contains a character USA fonts can't render "
            f"(latin-1 only): {e}"
        ) from e


def _read_data_section(ship: Afs, idx: dict[str, int], name: str, fh) -> bytes | None:
    if name.lower() not in idx:
        return None
    blob = ship.read_entry(idx[name.lower()], fh)
    try:
        _, _, data = parse_fpb_raw(blob)
    except ValueError:
        return None
    return data


def extract_all_fpb(usa_ship: Path, kr_ship: Path | None, jp_ship: Path | None,
                    out_dir: Path) -> int:
    """Extract every .fpb in USA SHIP into per-file JSON catalogs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    usa = Afs.open(usa_ship); usa_n = usa.read_filename_toc()
    kr_idx = jp_idx = None
    kr = jp = None
    if kr_ship and kr_ship.exists():
        kr = Afs.open(kr_ship); kr_n = kr.read_filename_toc()
        kr_idx = {n.lower(): i for i, n in enumerate(kr_n)}
    if jp_ship and jp_ship.exists():
        jp = Afs.open(jp_ship); jp_n = jp.read_filename_toc()
        jp_idx = {n.lower(): i for i, n in enumerate(jp_n)}

    written = 0
    fhs: dict[str, object] = {"usa": usa_ship.open("rb")}
    if kr_idx is not None: fhs["kr"] = kr_ship.open("rb")
    if jp_idx is not None: fhs["jp"] = jp_ship.open("rb")
    try:
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
                "en": _decode(usa_data, "latin-1"),
            }
            if kr_idx is not None:
                kr_data = _read_data_section(kr, kr_idx, name, fhs["kr"])
                if kr_data is not None:
                    cat["kr"] = _decode(kr_data, "cp949")
            if jp_idx is not None:
                jp_data = _read_data_section(jp, jp_idx, name, fhs["jp"])
                if jp_data is not None:
                    cat["jp"] = _decode(jp_data, "shift_jis")
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


# --------------------------------------------------------------------------- fixed-stride slot files

def _split_slot(slot: bytes, cap: int) -> tuple[bytes, int, bytes]:
    """Decompose a slot into ``(string_bytes, tail_at, tail_bytes)``.

    - ``string_bytes`` — leading bytes up to the first null terminator.
    - ``tail_at`` — offset where the engine-side trailing structure
      starts (= position of the first non-zero byte AFTER the string's
      null). When there's no trailing structure, equals ``cap``.
    - ``tail_bytes`` — verbatim ``slot[tail_at:cap]`` (preserved through
      rebuild so engine metadata isn't clobbered).

    A translator can grow the string up to ``tail_at - 1`` bytes (one
    spare byte for the null terminator) without touching the trailer.
    """
    s_end = slot.find(b"\x00", 0, cap)
    if s_end < 0:
        return slot[:cap], cap, b""
    tail_at = cap
    for i in range(s_end + 1, cap):
        if slot[i] != 0:
            tail_at = i
            break
    return slot[:s_end], tail_at, slot[tail_at:cap]


def parse_slot_file(blob: bytes, ext: str) -> tuple[bytes, list[tuple[bytes, int]]]:
    """Parse a fixed-stride slot file. Returns (header_bytes, [(slot_bytes, cap), ...]).

    The cap is normally ``slot_stride``; a file ending mid-stride yields
    a shorter cap for its trailing slot. The caller decodes the bytes
    and the cap drives null-padding on rebuild for byte-identical
    round-trips.
    """
    if ext not in SLOT_FORMATS:
        raise ValueError(f"unknown slot ext: {ext}")
    header_size, stride = SLOT_FORMATS[ext]
    if len(blob) < header_size:
        raise ValueError(f"{ext} too small ({len(blob)} B, need ≥{header_size})")
    header = blob[:header_size]
    body = blob[header_size:]
    slots: list[tuple[bytes, int]] = []
    pos = 0
    while pos < len(body):
        end = min(pos + stride, len(body))
        slots.append((body[pos:end], end - pos))
        pos = end
    return header, slots


def build_slot_file(
    header: bytes,
    slot_entries: list[tuple[bytes, int, int, bytes]],
    ext: str,
) -> bytes:
    """Re-emit a fixed-stride slot file.

    ``slot_entries`` is ``[(encoded_string, cap, tail_at, tail_bytes), ...]``:

    - ``encoded_string`` — translator's bytes for the leading string
      (latin-1, no null terminator).
    - ``cap`` — total slot capacity (= ``slot_stride`` for full slots,
      smaller for a trailing partial slot).
    - ``tail_at`` — offset within the slot where ``tail_bytes`` are
      written. The string + null terminator must fit within
      ``[0, tail_at)``; the gap between is null-padded.
    - ``tail_bytes`` — engine-side trailing structure preserved verbatim
      (``slot[tail_at:cap]`` from the original).

    A string exceeding ``tail_at - 1`` bytes raises ValueError so the
    caller surfaces it to the user.
    """
    if ext not in SLOT_FORMATS:
        raise ValueError(f"unknown slot ext: {ext}")
    _, stride = SLOT_FORMATS[ext]
    out = bytearray(header)
    for i, (s, cap, tail_at, tail_bytes) in enumerate(slot_entries):
        if cap > stride:
            raise ValueError(f"{ext} slot {i} cap {cap} > stride {stride}")
        if tail_at > cap:
            raise ValueError(f"{ext} slot {i} tail_at {tail_at} > cap {cap}")
        if len(tail_bytes) != cap - tail_at:
            raise ValueError(
                f"{ext} slot {i} tail length {len(tail_bytes)} != "
                f"cap-tail_at = {cap - tail_at}"
            )
        if len(s) >= tail_at:
            raise ValueError(
                f"{ext} slot {i} string exceeds editable space: "
                f"{len(s)} bytes (max {tail_at - 1}; remaining "
                f"{cap - tail_at} bytes are engine trailer that "
                f"can't be overwritten)."
            )
        out += s
        out += b"\x00" * (tail_at - len(s))
        out += tail_bytes
    return bytes(out)


def _slot_text(slot: bytes, encoding: str, cap: int) -> str:
    """Decode a slot's leading null-terminated string only."""
    string_bytes, _, _ = _split_slot(slot, cap)
    return string_bytes.decode(encoding, errors="replace")


def extract_all_slot(ext: str,
                     usa_ship: Path,
                     kr_ship: Path | None,
                     jp_ship: Path | None,
                     out_dir: Path) -> int:
    """Extract every file of `ext` in USA SHIP into per-file JSON catalogs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    usa = Afs.open(usa_ship); usa_n = usa.read_filename_toc()
    kr = jp = None
    kr_idx = jp_idx = None
    if kr_ship and kr_ship.exists():
        kr = Afs.open(kr_ship); kr_n = kr.read_filename_toc()
        kr_idx = {n.lower(): i for i, n in enumerate(kr_n)}
    if jp_ship and jp_ship.exists():
        jp = Afs.open(jp_ship); jp_n = jp.read_filename_toc()
        jp_idx = {n.lower(): i for i, n in enumerate(jp_n)}

    written = 0
    fhs: dict[str, object] = {"usa": usa_ship.open("rb")}
    if kr_idx is not None: fhs["kr"] = kr_ship.open("rb")
    if jp_idx is not None: fhs["jp"] = jp_ship.open("rb")
    try:
        for i, name in enumerate(usa_n):
            if not name.lower().endswith(ext):
                continue
            try:
                u_blob = usa.read_entry(i, fhs["usa"])
                u_hdr, u_slots = parse_slot_file(u_blob, ext)
            except ValueError:
                continue

            k_slots: list[tuple[bytes, int]] = []
            j_slots: list[tuple[bytes, int]] = []
            if kr_idx is not None and name.lower() in kr_idx:
                try:
                    k_blob = kr.read_entry(kr_idx[name.lower()], fhs["kr"])
                    _, k_slots = parse_slot_file(k_blob, ext)
                except ValueError:
                    pass
            if jp_idx is not None and name.lower() in jp_idx:
                try:
                    j_blob = jp.read_entry(jp_idx[name.lower()], fhs["jp"])
                    _, j_slots = parse_slot_file(j_blob, ext)
                except ValueError:
                    pass

            slots_out: list[dict] = []
            for k, (slot, cap) in enumerate(u_slots):
                _, tail_at, tail_bytes = _split_slot(slot, cap)
                rec: dict = {"i": k, "cap": cap, "tail_at": tail_at,
                             "en": _slot_text(slot, "latin-1", cap)}
                if tail_bytes:
                    rec["tail_hex"] = tail_bytes.hex()
                if k < len(k_slots):
                    ks, kc = k_slots[k]
                    rec["kr"] = _slot_text(ks, "cp949", kc)
                if k < len(j_slots):
                    js, jc = j_slots[k]
                    rec["jp"] = _slot_text(js, "shift_jis", jc)
                slots_out.append(rec)

            cat = {
                "file": name,
                "ext": ext,
                "header_hex": u_hdr.hex(),
                "slot_stride": SLOT_FORMATS[ext][1],
                "slots": slots_out,
            }
            (out_dir / f"{Path(name).stem}.json").write_text(
                json.dumps(cat, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            written += 1
    finally:
        for fh in fhs.values():
            fh.close()
    return written


def translated_slot_bytes(ext: str, name: str, usa_blob: bytes,
                          catalog_dir: Path | None = None) -> bytes | None:
    """If a translation catalog exists for `<basename><ext>`, build its bytes.

    Returns None when no catalog is present so the caller falls back to
    raw USA bytes (a half-translated repo still produces a working ISO).
    """
    if catalog_dir is None:
        catalog_dir = CATALOG_DIR / ext.lstrip(".")
    stem = Path(name).stem
    cat_path = catalog_dir / f"{stem}.json"
    if not cat_path.exists():
        return None
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    if cat.get("ext") != ext:
        raise ValueError(f"{cat_path}: catalog ext {cat.get('ext')!r} != {ext!r}")
    header = bytes.fromhex(cat["header_hex"])
    # Read original slots for cap + tail hints + fallback for untranslated entries.
    _, orig_slots = parse_slot_file(usa_blob, ext)
    by_index = {int(r["i"]): r for r in cat["slots"] if "i" in r}
    new_entries: list[tuple[bytes, int, int, bytes]] = []
    for k, (slot, cap) in enumerate(orig_slots):
        orig_string, orig_tail_at, orig_tail = _split_slot(slot, cap)
        rec = by_index.get(k)
        if rec is None or "en" not in rec:
            new_entries.append((orig_string, cap, orig_tail_at, orig_tail))
            continue
        s = _encode_en(rec["en"])
        # Allow per-slot overrides; fall back to original cap/tail.
        rec_cap = int(rec.get("cap", cap))
        rec_tail_at = int(rec.get("tail_at", orig_tail_at))
        rec_tail = bytes.fromhex(rec["tail_hex"]) if rec.get("tail_hex") else orig_tail
        new_entries.append((s, rec_cap, rec_tail_at, rec_tail))
    return build_slot_file(header, new_entries, ext)


# --------------------------------------------------------------------------- build hook

def translated_fpb_bytes(name: str, usa_blob: bytes,
                         catalog_dir: Path = CATALOG_DIR / "fpb") -> bytes | None:
    """If a translation catalog exists for `<basename>.fpb`, build its bytes.

    Returns None when no catalog is present (caller should fall back to
    the raw USA bytes). A half-translated repo still produces a working
    ISO — untranslated files keep their official USA English.
    """
    stem = Path(name).stem
    cat_path = catalog_dir / f"{stem}.json"
    if not cat_path.exists():
        return None
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    header16, orig_windows, orig_data = parse_fpb_raw(usa_blob)
    orig_en = orig_data.decode("latin-1")
    new_en = cat["en"]
    new_data = _encode_en(new_en)
    if new_en == orig_en:
        # No edit — preserve original windows verbatim
        windows = orig_windows
    else:
        # Edit detected — remap every window through old↔new diff so each
        # record still points at the right logical text in the new data.
        windows = remap_windows(orig_en, orig_windows, new_en)
    return build_fpb(header16, windows, new_data)


# --------------------------------------------------------------------------- CLI

def extract_all(usa_ship: Path, kr_ship: Path | None, jp_ship: Path | None,
                root_out_dir: Path = CATALOG_DIR) -> dict[str, int]:
    """Extract catalogs for every supported extension. Returns
    ``{ext: count}`` of files written per extension."""
    counts: dict[str, int] = {}
    counts[".fpb"] = extract_all_fpb(usa_ship, kr_ship, jp_ship,
                                     root_out_dir / "fpb")
    for ext in SLOT_FORMATS:
        counts[ext] = extract_all_slot(ext, usa_ship, kr_ship, jp_ship,
                                       root_out_dir / ext.lstrip("."))
    return counts


def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract", help="extract per-file translation catalogs")
    e.add_argument("--ext",
                   choices=["all"] + [x.lstrip(".") for x in SUPPORTED_EXTS],
                   default="all")
    e.add_argument("--out-root", type=Path, default=CATALOG_DIR)
    args = ap.parse_args()

    if args.cmd != "extract":
        return 2
    usa = ROOT / "work" / "usa" / "SHIP.AFS"
    kr  = ROOT / "work" / "kr"  / "SHIP.AFS"
    jp  = ROOT / "work" / "jp"  / "SHIP.AFS"
    if args.ext == "all":
        counts = extract_all(usa,
                             kr if kr.exists() else None,
                             jp if jp.exists() else None,
                             args.out_root)
    elif args.ext == "fpb":
        counts = {".fpb": extract_all_fpb(
            usa,
            kr if kr.exists() else None,
            jp if jp.exists() else None,
            args.out_root / "fpb")}
    else:
        ext = "." + args.ext
        counts = {ext: extract_all_slot(
            ext, usa,
            kr if kr.exists() else None,
            jp if jp.exists() else None,
            args.out_root / args.ext)}
    for ext, n in counts.items():
        print(f"  {ext}: wrote {n} catalog file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
