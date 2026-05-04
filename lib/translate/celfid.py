"""``celfid.lix`` translation catalog — independent, marker-keyed.

celfid.lix is the FILE.AFS startup bundle that holds the actual character
display names, item names, monster bestiary descriptions and several
hundred other game-text strings. Each is stored in a fixed-size
null-padded slot, preceded by a 16-byte structural fingerprint
(``\\xff\\xff\\xff\\xff <flags_u32> \\xff\\xff\\xff\\xff <id_u32>``) that's
language-independent and identical across USA / KR / JP.

We use that fingerprint as the slot's primary key in the catalog: find
the bytes in USA, search for the same bytes in KR / JP to pull
reference text, and at build time replace just the bytes after the
fingerprint with the translator's ``en`` (clamped to the slot's
null-padding capacity).

Catalog shape::

    {
      "file": "celfid.lix",
      "slots": [
        { "marker": "<32-hex>", "max_bytes": 31,
          "en": "Calintz", "kr": "칼린츠", "jp": "カリンツ" },
        ...
      ]
    }

Empty ``kr`` / ``jp`` are dropped (matches the SHIP-catalog convention
for records that don't exist in a given source region).
"""
from __future__ import annotations

import json
import re
import struct
import sys
import zlib
from pathlib import Path
from typing import Iterable

from cri_afs import Afs

from ._common import CATALOG_DIR, decode_safe, encode_en


CHUNK_SIZE = 24576

# Fixed-slot pattern: starts with uppercase letter, length ≥4, has lowercase
# or space, followed by ≥3 nulls. Matches real display strings, rejects
# 2-3 char garbage that happens to look null-padded.
_SLOT_RE = re.compile(rb"(?<=\x00)([A-Z][A-Za-z0-9 ',.!?:;\-\$]+)\x00{3,}")
_FF_MARKER = b"\xff\xff\xff\xff"


def decompress_chunked(blob: bytes) -> bytes:
    out = bytearray()
    pos = 0
    while pos + 8 <= len(blob):
        unc, cmp = struct.unpack_from("<II", blob, pos)
        if cmp == 0 or pos + 8 + cmp > len(blob):
            break
        out += zlib.decompress(blob[pos + 8 : pos + 8 + cmp])
        pos += 8 + cmp
    return bytes(out)


def recompress_chunked(decomp: bytes, chunk_size: int = CHUNK_SIZE) -> bytes:
    out = bytearray()
    for off in range(0, len(decomp), chunk_size):
        chunk = decomp[off : off + chunk_size]
        c = zlib.compress(chunk, level=9)
        out += struct.pack("<II", len(chunk), len(c))
        out += c
    return bytes(out)


def _read_celfid(file_afs_path: Path) -> bytes:
    afs = Afs.open(file_afs_path)
    n = afs.read_filename_toc()
    idx = {x.lower(): i for i, x in enumerate(n)}
    if "celfid.lix" not in idx:
        raise ValueError(f"{file_afs_path}: no celfid.lix entry")
    with file_afs_path.open("rb") as fh:
        return decompress_chunked(afs.read_entry(idx["celfid.lix"], fh))


def _string_at(blob: bytes, start: int, max_search: int = 200,
               encoding: str = "latin-1") -> tuple[str, int]:
    """Read text starting at ``start`` until first null byte, with capacity
    measured by walking through following nulls. Returns ``(text, padding_end)``
    where ``padding_end - start`` is the slot's max_bytes capacity."""
    null_at = blob.find(b"\x00", start, start + max_search)
    if null_at < 0:
        return "", start
    text = blob[start:null_at].decode(encoding, errors="replace")
    # Walk through the trailing nulls to measure capacity
    pos = null_at
    while pos < len(blob) and blob[pos] == 0:
        pos += 1
    # Cap the capacity at a reasonable max — runs of nulls in compressed-data
    # regions can be huge and aren't real "slot" padding
    return text, min(pos, start + 96)


_RUN_OF_LOWER_RE = re.compile(r"[a-z]{3,}")


def _find_slots(usa_decomp: bytes) -> list[dict]:
    """Walk USA's celfid.lix and return one record per filtered fixed-slot
    string. Each record carries the 16-byte structural marker preceding it.

    Junk filter intentionally aggressive: a real game string almost always
    has a run of ≥3 consecutive lowercase letters somewhere (a real word).
    Strings like ``"Px A"`` or ``"Cx F"`` that pass the bare regex but
    aren't real text get rejected."""
    out: list[dict] = []
    for m in _SLOT_RE.finditer(usa_decomp):
        text = m.group(1).decode("latin-1")
        if len(text) < 4:
            continue
        if not _RUN_OF_LOWER_RE.search(text):
            # Allow a couple structured exceptions: ALL-CAPS abbreviations
            # like "ACC", "ATK" that are real UI labels, IFF length is small
            if not (text.isupper() and 2 < len(text) <= 4):
                continue
        # Junk filter: too many non-letter chars (excluding spaces)
        letters = sum(1 for c in text if c.isalpha())
        non_letters = len(text) - text.count(" ") - letters
        if non_letters > 0.3 * len(text):
            continue
        marker = usa_decomp[max(0, m.start() - 16):m.start()]
        # Require structural marker presence
        if _FF_MARKER not in marker:
            continue
        # Capacity: slot's null-padding bytes after the string
        end = m.end()  # this is right after the run of trailing nulls already
        # Actually m.end() is past the last \x00 in the {3,} group. Recompute:
        s_end = m.start() + len(m.group(1))   # text end
        pos = s_end
        while pos < len(usa_decomp) and usa_decomp[pos] == 0 and pos < s_end + 96:
            pos += 1
        max_bytes = pos - m.start()
        out.append({
            "marker": marker.hex(),
            "max_bytes": max_bytes,
            "en": text,
            "_offset": m.start(),
        })
    return out


def extract_celfid_catalog(usa_file_afs: Path,
                           kr_file_afs: Path | None,
                           jp_file_afs: Path | None,
                           out_path: Path) -> int:
    """Extract a per-slot catalog from USA celfid.lix, with KR/JP refs cross-
    referenced via 16-byte structural marker. Empty kr/jp are dropped."""
    usa = _read_celfid(usa_file_afs)
    kr = _read_celfid(kr_file_afs) if kr_file_afs and kr_file_afs.exists() else b""
    jp = _read_celfid(jp_file_afs) if jp_file_afs and jp_file_afs.exists() else b""

    slots = _find_slots(usa)
    # Marker must uniquely identify the slot in the decompressed bytes.
    # Markers starting with many nulls or any other repeating pattern can
    # appear at multiple positions — find() would land on the wrong one
    # at patch time. Drop those slots; the catalog-keyed approach can't
    # safely round-trip them.
    kept: list[dict] = []
    for s in slots:
        marker_bytes = bytes.fromhex(s["marker"])
        if usa.count(marker_bytes) != 1:
            continue
        kept.append(s)
    slots = kept

    # Cross-reference KR/JP
    for slot in slots:
        marker = bytes.fromhex(slot["marker"])
        if kr:
            kr_idx = kr.find(marker)
            if kr_idx >= 0:
                kr_text, _ = _string_at(kr, kr_idx + len(marker), encoding="cp949")
                if kr_text:
                    slot["kr"] = kr_text
        if jp:
            jp_idx = jp.find(marker)
            if jp_idx >= 0:
                jp_text, _ = _string_at(jp, jp_idx + len(marker), encoding="shift_jis")
                if jp_text:
                    slot["jp"] = jp_text
        # Drop the internal _offset (used only for extraction order)
        del slot["_offset"]

    cat = {"file": "celfid.lix", "slots": slots}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cat, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return len(slots)


def translated_celfid_bytes(usa_file_afs: Path,
                            catalog_path: Path | None = None
                            ) -> bytes | None:
    """Build a patched celfid.lix from the catalog. Returns the (still
    compressed/chunked) celfid.lix bytes ready to be packed into FILE.AFS,
    or None if no catalog or no slot was edited (caller falls back to USA)."""
    if catalog_path is None:
        catalog_path = CATALOG_DIR / "celfid.json"
    if not catalog_path.exists():
        return None
    cat = json.loads(catalog_path.read_text(encoding="utf-8"))
    slots = cat.get("slots", [])

    decomp = _read_celfid(usa_file_afs)
    out = bytearray(decomp)

    any_edit = False
    edits = 0
    for rec in slots:
        marker = bytes.fromhex(rec["marker"])
        max_bytes = int(rec["max_bytes"])
        cat_en = rec.get("en", "")
        # Locate the slot by marker
        idx = bytes(out).find(marker)
        if idx < 0:
            continue
        slot_start = idx + len(marker)
        # Original USA text at this position
        usa_text, _ = _string_at(decomp, slot_start, encoding="latin-1")
        if cat_en == usa_text:
            continue
        # Edited — write the new bytes, null-padded to the slot capacity
        en_bytes = encode_en(cat_en)
        if len(en_bytes) > max_bytes - 1:  # 1 reserved for null terminator
            raise ValueError(
                f"celfid.lix slot (marker {rec['marker'][:16]}...): "
                f"edited string {len(en_bytes)} bytes > cap {max_bytes - 1}. "
                f"Trim translation."
            )
        out[slot_start:slot_start + max_bytes] = (
            en_bytes + b"\x00" * (max_bytes - len(en_bytes))
        )
        any_edit = True
        edits += 1

    if not any_edit:
        return None
    return recompress_chunked(bytes(out))
