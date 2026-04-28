"""Canonical hybrid SHIP.AFS builder for the Magna Carta undub.

Architecture
------------
Start from KR's SHIP.AFS as the structural base (so KR `.fld` scene scripts,
`.lpt` lipsync curves, `.btv` battle voice triggers, and other voice-binding
files reference filenames and IDs the engine will resolve correctly against
KR MUSIC.AFS). Override:

  1. Text-bearing extensions   → USA bytes (English UI/dialog/items).
  2. `.fpb` (PlayBook dialog)  → USA bytes (English world dialog).
  3. Slot-0 manifest           → rebuilt with the hybrid's actual sizes.

The manifest rebuild is the critical piece: SHIP.AFS slot 0 is the file
`AFSShipFileIndex.idx`, a plaintext `(filename, size_in_decimal)` table.
The engine reads this manifest at boot to populate its in-memory file-size
cache. It does NOT use the AFS primary TOC's u32 size field for that.

Without the manifest rebuild, every USA-overlaid file whose USA size differs
from KR's gets read by the engine with KR's expected size. For files where
USA is BIGGER than KR, the engine reads short (KR bytes worth of USA file),
and the parser then walks the file's larger internal structure, reading
past the buffer end → undefined behavior → crash.

With the manifest rebuilt, each entry is read with its ACTUAL size, and
swap is safe regardless of size direction.

Why KR-base
-----------
USA boot ELF + USA `MrtsGame.u` + USA fonts in `temple.utx` are kept (so
ASCII text renders correctly), but the AFS ASSETS (LINEAR.AFS, MUSIC.AFS,
SHIP.AFS) are KR-base because:
  - KR voice IDs in MUSIC.AFS are referenced by KR's `.fld` scripts.
  - KR's scene scripts internally reference KR's `.lpt`/`.btv`/`.lvt`/etc.
    by ID. Mixing in USA equivalents breaks scene state.
  - KR LINEAR.AFS holds level data that pairs with KR's scene scripts.

Text overlays (per extension) bring English back without disturbing the
voice/scene path.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from afs import Afs, write_afs


# Extensions that hold English text in USA / Korean text in KR.
# Swap to USA for English UI/dialog/items.
# All of these are structurally-shared (same record counts, same lookup
# scheme) between regions, so the only difference is the strings.
USA_TEXT_EXTS = (
    ".cht",  # phone conversations + NPC option dialog
    ".tui",  # UI labels
    ".itm",  # item names + descriptions
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


def build_manifest(entries: list[tuple[str, bytes]]) -> bytes:
    """Build the SHIP.AFS slot-0 manifest from entries' actual sizes.

    Format (plaintext, CRLF-delimited, ASCII):

      AFSShipFileIndex.idx\r\n
      0\r\n                       <- manifest's own size (placeholder; the
                                     engine reads this entry's real size
                                     from the AFS primary TOC)
      <name1>\r\n
      <size1_in_decimal>\r\n
      <name2>\r\n
      <size2_in_decimal>\r\n
      ...

    Entries[0] is expected to be the manifest itself; the manifest's own
    record at the top uses the placeholder "0" for size.
    """
    lines = [MANIFEST_NAME, "0"]
    for name, blob in entries:
        if name == MANIFEST_NAME:
            continue
        lines.append(name)
        lines.append(str(len(blob)))
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def build(out_path: Path = ROOT / "build" / "kr_base" / "SHIP.AFS",
          usa_ship: Path = ROOT / "work" / "usa" / "SHIP.AFS",
          kr_ship: Path = ROOT / "work" / "kr" / "SHIP.AFS",
          verbose: bool = True) -> Path:
    usa = Afs.open(usa_ship); usa_n = usa.read_filename_toc()
    kr = Afs.open(kr_ship);  kr_n = kr.read_filename_toc()
    usa_idx = {n.lower(): i for i, n in enumerate(usa_n)}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, bytes]] = []
    swapped_text = 0
    swapped_fpb = 0
    kept_kr = 0

    with kr_ship.open("rb") as fh_kr, usa_ship.open("rb") as fh_usa:
        for i, name in enumerate(kr_n):
            lname = name.lower()
            ext = "." + lname.rsplit(".", 1)[-1] if "." in lname else ""

            if SWAP_FPB and ext == ".fpb" and lname in usa_idx:
                blob = usa.read_entry(usa_idx[lname], fh_usa)
                swapped_fpb += 1
            elif ext in USA_TEXT_EXTS and lname in usa_idx:
                blob = usa.read_entry(usa_idx[lname], fh_usa)
                swapped_text += 1
            else:
                blob = kr.read_entry(i, fh_kr)
                kept_kr += 1
            entries.append((name, blob))

    # Rebuild the slot-0 manifest with the actual sizes of bytes we wrote.
    # This is the key step: without it, the engine uses KR's manifest sizes
    # and breaks on USA-bigger files.
    new_manifest = build_manifest(entries)
    assert entries[0][0] == MANIFEST_NAME, (
        f"slot 0 should be {MANIFEST_NAME}, got {entries[0][0]}"
    )
    if verbose:
        print(f"  manifest: KR original {kr.entries[0].size:,} B → "
              f"rebuilt {len(new_manifest):,} B")
    entries[0] = (MANIFEST_NAME, new_manifest)

    # IMPORTANT: pass through KR's TOC metadata as-is — the entry list and
    # order is unchanged from KR base, so KR's metadata (16-byte trailers)
    # still aligns by index.
    kr_meta = kr.read_toc_metadata()

    if verbose:
        print(f"  total entries: {len(entries)}")
        print(f"  USA text overlays ({len(USA_TEXT_EXTS)} exts): {swapped_text}")
        print(f"  .fpb blanket-swapped to USA: {swapped_fpb}")
        print(f"  kept KR: {kept_kr}")

    write_afs(out_path, entries, toc_metadata=kr_meta)
    if verbose:
        print(f"  wrote {out_path} ({out_path.stat().st_size:,} B)")
    return out_path


if __name__ == "__main__":
    build()
