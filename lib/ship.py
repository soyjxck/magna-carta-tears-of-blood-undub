"""Canonical hybrid SHIP.AFS builder for the Magna Carta undub.

Architecture
------------
Start from a source region's SHIP.AFS as the structural base — KR or JP —
so that region's `.fld` scene scripts, `.lpt` lipsync curves, `.btv` battle
voice triggers, and other voice-binding files reference filenames and IDs
the engine will resolve correctly against the matching region's MUSIC.AFS.
Override:

  1. Text-bearing extensions   → USA bytes (English UI/dialog/items).
  2. `.fpb` (PlayBook dialog)  → USA bytes (English world dialog).
  3. Slot-0 manifest           → rebuilt with the hybrid's actual sizes.

The manifest rebuild is the critical piece: SHIP.AFS slot 0 is the file
`AFSShipFileIndex.idx`, a plaintext `(filename, size_in_decimal)` table.
The engine reads this manifest at boot to populate its in-memory file-size
cache. It does NOT use the AFS primary TOC's u32 size field for that.

Without the manifest rebuild, every USA-overlaid file whose USA size
differs from the source region's gets read by the engine with the source
region's expected size. For files where USA is BIGGER, the engine reads
short, and the parser walks the file's larger internal structure, reading
past the buffer end → undefined behavior → crash.

Why source-region-base
----------------------
USA boot ELF + USA `MrtsGame.u` + USA fonts in `temple.utx` are kept (so
ASCII text renders correctly), but the AFS ASSETS (LINEAR.AFS, MUSIC.AFS,
SHIP.AFS) come from the source region because:
  - The source region's voice IDs in MUSIC.AFS are referenced by its own
    `.fld` scripts.
  - Its scene scripts internally reference its own `.lpt`/`.btv`/`.lvt`/etc.
    by ID. Mixing in USA equivalents breaks scene state.
  - Its LINEAR.AFS holds level data that pairs with its scene scripts.

Text overlays (per extension) bring English back without disturbing the
voice/scene path.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path

from cri_afs import Afs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from afs import filename_toc, finalize_hybrid_afs
from translate import (
    REGION_OVERLAY_EXTS,
    SLOT_FORMATS,
    translated_fpb_bytes,
    translated_region_bytes,
    translated_slot_bytes,
)

# Extensions that hold English text in USA / Korean text in KR.
# Swap to USA for English UI/dialog/items.
# All of these are structurally-shared (same record counts, same lookup
# scheme) between regions, so the only difference is the strings.
USA_TEXT_EXTS = (
    ".cht",  # phone conversations + NPC option dialog
    ".pod",  # phone conversation popup tutorials (148 files, 12328 B fixed)
    ".tui",  # UI labels
    ".itm",  # item names + descriptions
    ".odd",  # side quests + chest reward names (e.g. "Riot Sword", "Vorpal Bunny")
    ".gft",  # gift dialog
    ".cha",  # character data (names, bios)
    ".abi",  # ability definitions
    ".sgi",  # combat-style descriptions
    ".cdg",  # talisman effects
    ".mdg",  # monster bestiary
    ".dod",  # character titles
    ".cls",  # class definitions
    ".ecd",  # event/cutscene dialog
    ".att",  # attribute/stat data
    ".nod",  # area names
    ".val",  # value data
    ".fds",  # friend/team dialog
)


# `.fpb` (PlayBook dialog tables) are blanket-swapped to USA. The slot-0
# manifest rebuild ensures the engine reads each file at its actual size,
# so size mismatches no longer crash the parser.
SWAP_FPB = True


MANIFEST_NAME = "AFSShipFileIndex.idx"


def _pick_entry_blob(name: str, ext: str, usa_blob: bytes | None,
                     src_blob_reader: Callable[[], bytes],
                     translations_dir: Path | None
                     ) -> tuple[bytes, str]:
    """Decide what bytes to write for one SHIP entry. Returns
    ``(blob, kind)`` where ``kind`` is one of:

      ``"translated_fpb"``    `.fpb` rebuilt from a translation catalog
      ``"swapped_fpb"``       `.fpb` raw USA bytes (catalog absent / disabled)
      ``"translated_slot"``   slot-format file rebuilt from catalog
      ``"translated_region"`` region-overlay file rebuilt from catalog
      ``"swapped_text"``      one of USA_TEXT_EXTS, raw USA bytes
      ``"kept_src"``          source-region passthrough (voice/scene path)

    `usa_blob` is None when the entry has no USA counterpart — caller
    should keep source bytes in that case.
    """
    if usa_blob is None:
        return src_blob_reader(), "kept_src"

    if SWAP_FPB and ext == ".fpb":
        if translations_dir is not None:
            tx = translated_fpb_bytes(name, usa_blob,
                                      catalog_dir=translations_dir / "fpb")
            if tx is not None:
                return tx, "translated_fpb"
        return usa_blob, "swapped_fpb"

    if ext in USA_TEXT_EXTS:
        if translations_dir is not None:
            if ext in SLOT_FORMATS:
                tx = translated_slot_bytes(
                    ext, name, usa_blob,
                    catalog_dir=translations_dir / ext.lstrip("."))
                if tx is not None:
                    return tx, "translated_slot"
            elif ext in REGION_OVERLAY_EXTS:
                tx = translated_region_bytes(
                    ext, name, usa_blob,
                    catalog_dir=translations_dir / ext.lstrip("."))
                if tx is not None:
                    return tx, "translated_region"
        return usa_blob, "swapped_text"

    return src_blob_reader(), "kept_src"


def build(out_path: Path | None = None,
          usa_ship: Path = ROOT / "work" / "usa" / "SHIP.AFS",
          src_ship: Path = ROOT / "work" / "kr" / "SHIP.AFS",
          translations_dir: Path | None = None,
          verbose: bool = True) -> Path:
    """Build a hybrid SHIP.AFS using `src_ship` (KR or JP) as the structural
    base, with USA bytes overlaid for text-bearing extensions and `.fpb`.

    `translations_dir`: when set, files with a matching catalog at
    `<dir>/<ext>/<basename>.json` are rebuilt from that catalog
    (translation flow). When None, raw USA bytes are used (default —
    vanilla undub)."""
    if out_path is None:
        # Default: build/<region>_base/SHIP.AFS (e.g. work/jp/... -> build/jp_base/...)
        region_tag = Path(src_ship).parent.name + "_base"
        out_path = ROOT / "build" / region_tag / "SHIP.AFS"

    usa = Afs.open(usa_ship)
    src = Afs.open(src_ship)
    src_n = filename_toc(src)
    usa_n = filename_toc(usa)
    usa_idx = {n.lower(): i for i, n in enumerate(usa_n)}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, bytes]] = []
    counts: dict[str, int] = {}

    with src_ship.open("rb") as fh_src, usa_ship.open("rb") as fh_usa:
        for i, name in enumerate(src_n):
            lname = name.lower()
            ext = "." + lname.rsplit(".", 1)[-1] if "." in lname else ""
            usa_blob = (usa.read_entry(usa_idx[lname], fh_usa)
                        if lname in usa_idx else None)
            blob, kind = _pick_entry_blob(
                name, ext, usa_blob,
                partial(src.read_entry, i, fh_src),
                translations_dir,
            )
            counts[kind] = counts.get(kind, 0) + 1
            entries.append((name, blob))

    def _report() -> None:
        print(f"  source: {src_ship}")
        print(f"  total entries: {len(entries)}")
        print(f"  USA text overlays ({len(USA_TEXT_EXTS)} exts): "
              f"{counts.get('swapped_text', 0)}")
        print(f"  .fpb blanket-swapped to USA: {counts.get('swapped_fpb', 0)}")
        for kind, label in (
            ("translated_fpb",    ".fpb built from translation catalog"),
            ("translated_slot",   "slot-format files built from translation catalog"),
            ("translated_region", "region-overlay files built from translation catalog"),
        ):
            if counts.get(kind):
                print(f"  {label}: {counts[kind]}")
        print(f"  kept source-region: {counts.get('kept_src', 0)}")

    # Rebuild the slot-0 manifest with the hybrid's actual sizes (the keystone
    # step — see afs.py), then pass through the source TOC and write.
    return finalize_hybrid_afs(
        out_path, src, entries,
        manifest_name=MANIFEST_NAME, header=MANIFEST_NAME,
        verbose=verbose, report=_report,
    )


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["kr", "jp"], default="kr",
                    help="source region for the hybrid (default: kr)")
    args = ap.parse_args()
    build(src_ship=ROOT / "work" / args.source / "SHIP.AFS")
