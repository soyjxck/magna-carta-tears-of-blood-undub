"""Fixed-stride slot files — ``.cht .odd .gft .cha .cdg .mdg .ecd .fds``.

Each file has a short header followed by ``count`` slots of fixed byte
stride. Each slot holds one null-terminated string padded with zeros;
some slots also have an engine-side trailer at the end (e.g. ``.cht``'s
16-byte option-ID block) which we preserve verbatim on rebuild.

Per-extension ``(header_size, slot_stride)`` was verified empirically
against every USA SHIP.AFS file of that ext — the engine bytecode
hardcodes the stride per format, so it's safe to use as a constant.

A translator can grow each slot's string up to ``tail_at - 1`` bytes
(``tail_at`` = where the engine trailer starts in the slot, == ``cap``
when there's no trailer).
"""
from __future__ import annotations

import json
from pathlib import Path

from ._common import (CATALOG_DIR, decode_safe, encode_en, open_ship_handles)


# header_size + count * slot_stride per extension
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


# --------------------------------------------------------------------------- parse / build

def split_slot(slot: bytes, cap: int) -> tuple[bytes, int, bytes]:
    """Decompose a slot into ``(string_bytes, tail_at, tail_bytes)``.

    - ``string_bytes`` — leading bytes up to the first null terminator.
    - ``tail_at`` — offset where the engine-side trailing structure
      starts (= the first non-zero byte after the string's null).
      Equals ``cap`` when the slot has no trailer.
    - ``tail_bytes`` — verbatim ``slot[tail_at:cap]``, preserved through
      rebuild so engine metadata isn't clobbered.

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


def parse_slot_file(blob: bytes, ext: str
                    ) -> tuple[bytes, list[tuple[bytes, int]]]:
    """Parse a fixed-stride slot file. Returns ``(header_bytes, [(slot_bytes, cap), ...])``.

    The cap is normally ``slot_stride``; a file ending mid-stride
    (sometimes the case for the trailing slot) yields a shorter cap.
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


def build_slot_file(header: bytes,
                    slot_entries: list[tuple[bytes, int, int, bytes]],
                    ext: str) -> bytes:
    """Re-emit a fixed-stride slot file.

    ``slot_entries`` is ``[(encoded_string, cap, tail_at, tail_bytes), ...]``.
    A string exceeding ``tail_at - 1`` bytes raises ValueError; the
    caller surfaces that to the translator.
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
    """Decode a slot's leading null-terminated string only. Trailer
    bytes (if any) are not part of the displayable text."""
    string_bytes, _, _ = split_slot(slot, cap)
    return string_bytes.decode(encoding, errors="replace")


# --------------------------------------------------------------------------- catalog I/O

def extract_all_slot(ext: str,
                     usa_ship: Path,
                     kr_ship: Path | None,
                     jp_ship: Path | None,
                     out_dir: Path) -> int:
    """Extract every file of ``ext`` in USA SHIP into per-file JSON catalogs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    usa, usa_idx, kr_pair, jp_pair, fhs = open_ship_handles(
        usa_ship, kr_ship, jp_ship)
    written = 0
    try:
        usa_n = usa.read_filename_toc()
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
            if kr_pair is not None and name.lower() in kr_pair[1]:
                try:
                    k_blob = kr_pair[0].read_entry(
                        kr_pair[1][name.lower()], fhs["kr"])
                    _, k_slots = parse_slot_file(k_blob, ext)
                except ValueError:
                    pass
            if jp_pair is not None and name.lower() in jp_pair[1]:
                try:
                    j_blob = jp_pair[0].read_entry(
                        jp_pair[1][name.lower()], fhs["jp"])
                    _, j_slots = parse_slot_file(j_blob, ext)
                except ValueError:
                    pass

            slots_out: list[dict] = []
            for k, (slot, cap) in enumerate(u_slots):
                _, tail_at, tail_bytes = split_slot(slot, cap)
                rec: dict = {"i": k, "cap": cap, "tail_at": tail_at,
                             "en": _slot_text(slot, "latin-1", cap)}
                if tail_bytes:
                    rec["tail_hex"] = tail_bytes.hex()
                if k < len(k_slots):
                    rec["kr"] = _slot_text(k_slots[k][0], "cp949", k_slots[k][1])
                if k < len(j_slots):
                    rec["jp"] = _slot_text(j_slots[k][0], "shift_jis", j_slots[k][1])
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
    """Build slot-format bytes from a translation catalog, or return None
    when no catalog exists (caller falls back to raw USA bytes)."""
    if catalog_dir is None:
        catalog_dir = CATALOG_DIR / ext.lstrip(".")
    cat_path = catalog_dir / f"{Path(name).stem}.json"
    if not cat_path.exists():
        return None
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    if cat.get("ext") != ext:
        raise ValueError(f"{cat_path}: catalog ext {cat.get('ext')!r} != {ext!r}")
    header = bytes.fromhex(cat["header_hex"])
    _, orig_slots = parse_slot_file(usa_blob, ext)
    by_index = {int(r["i"]): r for r in cat["slots"] if "i" in r}
    new_entries: list[tuple[bytes, int, int, bytes]] = []
    for k, (slot, cap) in enumerate(orig_slots):
        orig_string, orig_tail_at, orig_tail = split_slot(slot, cap)
        rec = by_index.get(k)
        if rec is None or "en" not in rec:
            new_entries.append((orig_string, cap, orig_tail_at, orig_tail))
            continue
        s = encode_en(rec["en"])
        rec_cap = int(rec.get("cap", cap))
        rec_tail_at = int(rec.get("tail_at", orig_tail_at))
        rec_tail = bytes.fromhex(rec["tail_hex"]) if rec.get("tail_hex") else orig_tail
        new_entries.append((s, rec_cap, rec_tail_at, rec_tail))
    return build_slot_file(header, new_entries, ext)
