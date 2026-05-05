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


def _detect_linked_groups(slots: list[dict]) -> tuple[list[dict], list[dict]]:
    """Cluster slots whose strings cross-reference each other via shared
    substrings. Returns ``(linked_groups, leaf_slots)``.

    Why: changing one slot of a cross-referenced cluster (e.g. the title
    ``T.Roxy`` while leaving the base name ``Roxy`` alone) creates a
    dangling reference and crashes the engine on boot. Group them so the
    translator can edit a single ``base`` field and the build cascades the
    rename to every member via per-slot ``template`` strings.

    Heuristic for valid base candidates:
      * length ≥ 4 (avoid common 3-letter words like 'the')
      * starts with uppercase (proper noun / display name)
      * single word — no spaces, no sentence punctuation
      * appears as the entire ``en`` of at least one slot AND as a
        substring in at least one other slot's ``en``
      * appears at most once per containing slot (ambiguous templates
        rejected)
    """
    en_to_indices: dict[str, list[int]] = {}
    for i, s in enumerate(slots):
        en_to_indices.setdefault(s["en"], []).append(i)

    candidates: list[str] = []
    for en, indices in en_to_indices.items():
        if len(en) < 4:
            continue
        if not en[0].isupper():
            continue
        if any(c in en for c in ' .?!,;:'):
            continue
        # Must be referenced by at least one other slot
        n_refs = sum(1 for j, s in enumerate(slots)
                     if j not in indices and en in s["en"])
        if n_refs > 0:
            candidates.append(en)

    # Longest first so longer names take precedence over their substrings
    candidates.sort(key=lambda b: (-len(b), b))

    in_group: set[int] = set()
    groups: list[dict] = []
    for base in candidates:
        members: list[int] = []
        for i, s in enumerate(slots):
            if i in in_group:
                continue
            if base not in s["en"]:
                continue
            # Reject ambiguous templates (base appears multiple times in en)
            if s["en"].count(base) > 1:
                continue
            members.append(i)
        if len(members) < 2:
            continue
        group = {"base": base, "slots": []}
        for i in members:
            t = slots[i]
            entry = {
                "marker": t["marker"],
                "max_bytes": t["max_bytes"],
                "template": t["en"].replace(base, "{base}", 1),
            }
            if "kr" in t:
                entry["kr"] = t["kr"]
            if "jp" in t:
                entry["jp"] = t["jp"]
            group["slots"].append(entry)
            in_group.add(i)
        groups.append(group)

    # Pass 2: catch identical-string duplicates that the substring pass missed.
    # Pure duplicates (e.g. 'Reith' appearing as the full en in 3 different
    # slots, never as a substring) form a same-template group with all
    # members templated as "{base}". Editing the base renames every
    # duplicate together so cross-references stay consistent.
    by_text: dict[str, list[int]] = {}
    for i, s in enumerate(slots):
        if i in in_group:
            continue
        if not s.get("en") or len(s["en"]) < 4:
            continue
        if not s["en"][0].isupper():
            continue
        if any(c in s["en"] for c in '.?!,;:'):
            continue
        by_text.setdefault(s["en"], []).append(i)

    for text, indices in by_text.items():
        if len(indices) < 2:
            continue
        group = {"base": text, "slots": []}
        for i in indices:
            t = slots[i]
            entry = {
                "marker": t["marker"],
                "max_bytes": t["max_bytes"],
                "template": "{base}",
            }
            if "kr" in t:
                entry["kr"] = t["kr"]
            if "jp" in t:
                entry["jp"] = t["jp"]
            group["slots"].append(entry)
            in_group.add(i)
        groups.append(group)

    leaves = [slots[i] for i in range(len(slots)) if i not in in_group]
    return groups, leaves


def extract_celfid_catalog(usa_file_afs: Path,
                           kr_file_afs: Path | None,
                           jp_file_afs: Path | None,
                           out_path: Path) -> int:
    """Extract a celfid.lix catalog with USA + KR/JP refs cross-referenced
    via the 16-byte structural marker. Slots that cross-reference each
    other (e.g. ``Roxy`` and ``T.Roxy``) get clustered into
    ``linked_groups`` so the translator edits one ``base`` and the build
    cascades the rename to every member via templates. Stand-alone slots
    go in ``slots``."""
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

    linked_groups, leaves = _detect_linked_groups(slots)
    cat = {
        "file": "celfid.lix",
        "linked_groups": linked_groups,
        "slots": leaves,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(cat, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    return len(leaves) + sum(len(g["slots"]) for g in linked_groups)


def _patch_slot(out: bytearray, decomp: bytes, marker_hex: str,
                max_bytes: int, new_en: str) -> bool:
    """Locate the slot by marker, write ``new_en`` (null-padded to the slot
    capacity). Returns True if any byte changed, False if new_en already
    matches the existing bytes."""
    marker = bytes.fromhex(marker_hex)
    idx = bytes(out).find(marker)
    if idx < 0:
        return False
    slot_start = idx + len(marker)
    usa_text, _ = _string_at(decomp, slot_start, encoding="latin-1")
    if new_en == usa_text:
        return False
    en_bytes = encode_en(new_en)
    if len(en_bytes) > max_bytes - 1:   # 1 byte reserved for null terminator
        raise ValueError(
            f"celfid.lix slot (marker {marker_hex[:16]}...): "
            f"edited string {len(en_bytes)} bytes > cap {max_bytes - 1}. "
            f"Trim translation."
        )
    out[slot_start:slot_start + max_bytes] = (
        en_bytes + b"\x00" * (max_bytes - len(en_bytes))
    )
    return True


def translated_celfid_bytes(usa_file_afs: Path,
                            catalog_path: Path | None = None
                            ) -> bytes | None:
    """Build a patched celfid.lix from the catalog. Returns the (still
    compressed/chunked) celfid.lix bytes ready to be packed into FILE.AFS,
    or None if no catalog or no slot was edited.

    Two slot types are handled:
      * **Standalone slots** (``cat["slots"]``): each has its own ``en``;
        edit fires when ``en`` differs from USA's bytes at that position.
      * **Linked groups** (``cat["linked_groups"]``): each group has a
        ``base`` string and a list of member slots, each with a
        ``template`` like ``"T.{base}"`` or ``"{base}"``. Edit fires
        when ``base`` differs from the USA-original base; on edit, the
        new ``en`` for each member is computed by ``template.format(base=new_base)``
        and cascaded to every slot in the group consistently.
    """
    if catalog_path is None:
        catalog_path = CATALOG_DIR / "celfid.json"
    if not catalog_path.exists():
        return None
    cat = json.loads(catalog_path.read_text(encoding="utf-8"))
    leaves = cat.get("slots", [])
    groups = cat.get("linked_groups", [])

    decomp = _read_celfid(usa_file_afs)
    out = bytearray(decomp)
    any_edit = False

    # Stand-alone slots
    for rec in leaves:
        new_en = rec.get("en", "")
        if _patch_slot(out, decomp,
                       rec["marker"], int(rec["max_bytes"]), new_en):
            any_edit = True

    # Linked groups: derive USA's base from any "{base}" template member,
    # only cascade if the catalog's ``base`` differs.
    for grp in groups:
        new_base = grp.get("base", "")
        # Find USA's original base by reading any member where template == "{base}"
        usa_base = None
        for member in grp["slots"]:
            tmpl = member.get("template", "")
            if tmpl == "{base}":
                marker = bytes.fromhex(member["marker"])
                idx = decomp.find(marker)
                if idx >= 0:
                    usa_base, _ = _string_at(decomp, idx + len(marker), encoding="latin-1")
                    break
        if usa_base is None:
            # No clean "{base}" anchor — derive from first member by reversing template
            first = grp["slots"][0]
            tmpl = first["template"]
            marker = bytes.fromhex(first["marker"])
            idx = decomp.find(marker)
            if idx >= 0:
                full, _ = _string_at(decomp, idx + len(marker), encoding="latin-1")
                # Replace {base} with regex-friendly capture
                import re as _re
                rx = _re.escape(tmpl).replace(r"\{base\}", "(.+)")
                m = _re.fullmatch(rx, full)
                if m:
                    usa_base = m.group(1)

        if usa_base == new_base:
            continue   # no group-level edit

        for member in grp["slots"]:
            new_en = member["template"].format(base=new_base)
            if _patch_slot(out, decomp, member["marker"],
                           int(member["max_bytes"]), new_en):
                any_edit = True

        # Additionally byte-replace any remaining word-boundary occurrences of
        # the old base across the whole celfid.lix — catches UE2 name table
        # entries (`C_Calintz`, `Calintz_PartyData`, etc.) and other
        # non-slot-shaped references the engine cross-validates at boot.
        # Only safe when new_base has the same byte length as usa_base, since
        # name-table entries have fixed length-prefix bytes that would need
        # updating otherwise.
        if (usa_base and new_base and len(usa_base) == len(new_base)
                and all(c < 0x80 for c in new_base.encode("latin-1", errors="replace"))):
            old_b = usa_base.encode("latin-1")
            new_b = new_base.encode("latin-1")
            # Word-boundary replace: not preceded/followed by an alphanumeric
            pat = re.compile(rb'(?<![A-Za-z0-9])' + re.escape(old_b) + rb'(?![A-Za-z0-9])')
            new_bytes = pat.sub(new_b, bytes(out))
            if new_bytes != bytes(out):
                out = bytearray(new_bytes)
                any_edit = True

    if not any_edit:
        return None
    return recompress_chunked(bytes(out))
