"""Region-overlay format — ``.pod .tui .itm .abi .sgi .nod .dod .cls .att .val``.

These files mix binary structure (counts, IDs, sentinels, padding) with
ASCII text strings at fixed offsets. Rather than decode each format's
specific structure, we treat them generically:

  - **Extract**: scan USA bytes for runs of printable ASCII (≥4 chars).
    Each run is an editable text region with its own ``offset``,
    ``length``, and ``cap`` (how far it can grow before hitting the
    next non-zero structural byte).
  - **Repack**: copy the original USA blob and overwrite each catalog
    region with the translator's encoded ``en``, null-padded to ``cap``.
    Everything else (sentinels, headers, numeric tables) is preserved
    verbatim.

The translator can grow strings up to ``cap`` per region without
clobbering engine metadata.
"""
from __future__ import annotations

import json
from pathlib import Path

from ._common import (CATALOG_DIR, decode_safe, encode_en, open_ship_handles)


REGION_OVERLAY_EXTS = (".pod", ".tui", ".itm", ".abi", ".sgi", ".nod",
                       ".dod", ".cls", ".att", ".val")
REGION_MIN_LENGTH = 4


# --------------------------------------------------------------------------- region detection

def find_text_regions(blob: bytes,
                      min_length: int = REGION_MIN_LENGTH
                      ) -> list[tuple[int, int]]:
    """Locate runs of printable ASCII (0x20–0x7E) of length ≥ ``min_length``.

    Returns ``[(offset, length), ...]``. These are the editable regions;
    everything else in the file is treated as opaque binary structure.
    """
    regions: list[tuple[int, int]] = []
    pos = 0
    n = len(blob)
    while pos < n:
        while pos < n and not (0x20 <= blob[pos] < 0x7f):
            pos += 1
        start = pos
        while pos < n and 0x20 <= blob[pos] < 0x7f:
            pos += 1
        if pos - start >= min_length:
            regions.append((start, pos - start))
    return regions


def _region_capacity(blob: bytes, start: int, length: int,
                     next_region_start: int | None) -> int:
    """How many bytes a region can grow to without touching binary structure.

    Walks forward from ``start + length`` while bytes are null until
    either (a) a non-zero byte (= structure) appears or (b) we reach
    the next region's start, whichever comes first.
    """
    end = next_region_start if next_region_start is not None else len(blob)
    pos = start + length
    while pos < end and blob[pos] == 0:
        pos += 1
    return pos - start


# --------------------------------------------------------------------------- catalog I/O

def _src_text_at(blob: bytes | None, offset: int, cap: int,
                 encoding: str) -> str | None:
    """Decode source-region text at ``offset`` (USA's offset). Best-effort:
    KR/JP byte length at the same logical position may differ from USA's,
    so we read up to ``cap`` bytes and stop at the first null."""
    if blob is None or offset >= len(blob):
        return None
    src_len = min(cap, len(blob) - offset)
    src_bytes = blob[offset : offset + src_len].split(b"\x00", 1)[0]
    return src_bytes.decode(encoding, errors="replace")


def _detect_regions(blob: bytes) -> list[tuple[int, int, int]]:
    """Return ``[(offset, length, cap), ...]`` for every detected text region.

    Wraps ``find_text_regions`` and computes per-region capacity (how far
    the string can grow before hitting the next non-zero structural byte).
    The build path uses this to know where to write each catalog entry's
    ``en`` and how much room is available.
    """
    regions = find_text_regions(blob)
    out: list[tuple[int, int, int]] = []
    for k, (offset, length) in enumerate(regions):
        next_start = regions[k + 1][0] if k + 1 < len(regions) else None
        cap = _region_capacity(blob, offset, length, next_start)
        out.append((offset, length, cap))
    return out


def extract_all_region(ext: str,
                       usa_ship: Path,
                       kr_ship: Path | None,
                       jp_ship: Path | None,
                       out_dir: Path) -> int:
    """Extract every USA file of ``ext`` into a per-file JSON catalog.

    Catalog shape (one region per array entry; array index = region index)::

        {
          "file": "<name>.<ext>",
          "ext": "<ext>",
          "regions": [
            {"en": "...", "kr": "...", "jp": "..."},
            ...
          ]
        }

    All structural fields (offset, length, cap, file size) are derived from
    USA bytes at rebuild time — the translator only sees the text.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    usa, usa_idx, kr_pair, jp_pair, fhs = open_ship_handles(
        usa_ship, kr_ship, jp_ship)
    written = 0
    try:
        usa_n = usa.read_filename_toc()
        for i, name in enumerate(usa_n):
            if not name.lower().endswith(ext):
                continue
            usa_blob = usa.read_entry(i, fhs["usa"])
            regions = _detect_regions(usa_blob)
            if not regions:
                continue
            kr_blob = (kr_pair[0].read_entry(kr_pair[1][name.lower()], fhs["kr"])
                       if kr_pair is not None and name.lower() in kr_pair[1]
                       else None)
            jp_blob = (jp_pair[0].read_entry(jp_pair[1][name.lower()], fhs["jp"])
                       if jp_pair is not None and name.lower() in jp_pair[1]
                       else None)

            cat_regions: list[dict] = []
            for offset, length, cap in regions:
                rec: dict = {
                    "en": decode_safe(usa_blob[offset:offset + length], "latin-1"),
                }
                kr_text = _src_text_at(kr_blob, offset, cap, "cp949")
                if kr_text is not None:
                    rec["kr"] = kr_text
                jp_text = _src_text_at(jp_blob, offset, cap, "shift_jis")
                if jp_text is not None:
                    rec["jp"] = jp_text
                cat_regions.append(rec)

            cat = {"file": name, "ext": ext, "regions": cat_regions}
            (out_dir / f"{Path(name).stem}.json").write_text(
                json.dumps(cat, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            written += 1
    finally:
        for fh in fhs.values():
            fh.close()
    return written


def translated_region_bytes(ext: str, name: str, usa_blob: bytes,
                            catalog_dir: Path | None = None
                            ) -> bytes | None:
    """Apply catalog edits to ``usa_blob``. Returns None when no catalog
    exists OR no region was actually edited (caller falls back to USA —
    preserves byte-identical round-trip).

    Region offsets and caps are read fresh from USA bytes; only the
    translator's ``en`` is taken from the catalog.
    """
    if catalog_dir is None:
        catalog_dir = CATALOG_DIR / ext.lstrip(".")
    cat_path = catalog_dir / f"{Path(name).stem}.json"
    if not cat_path.exists():
        return None
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    if cat.get("ext") != ext:
        raise ValueError(f"{cat_path}: catalog ext {cat.get('ext')!r} != {ext!r}")

    usa_regions = _detect_regions(usa_blob)
    cat_regions = cat.get("regions", [])

    # Edit detection: any region whose en differs from USA?
    if len(cat_regions) == len(usa_regions):
        any_edit = False
        for rec, (offset, length, _cap) in zip(cat_regions, usa_regions):
            usa_en = usa_blob[offset:offset + length].decode(
                "latin-1", errors="replace")
            if rec.get("en", "") != usa_en:
                any_edit = True
                break
        if not any_edit:
            return None

    out = bytearray(usa_blob)
    for k, ((offset, _length, cap), rec) in enumerate(zip(usa_regions, cat_regions)):
        if "en" not in rec:
            continue
        s = encode_en(rec["en"])
        if len(s) > cap:
            raise ValueError(
                f"{ext} {name} region {k} (offset {offset}): edited string "
                f"{len(s)} bytes > cap {cap}. Trim the translation."
            )
        out[offset : offset + cap] = s + b"\x00" * (cap - len(s))
    return bytes(out)
