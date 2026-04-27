"""KR-base SHIP.AFS hybrid: keep KR's exact entry list, manifest, and TOC
metadata, then substitute USA bytes for shared filenames whose extension
carries English text. The voice/lipsync/scene path stays Korean and
internally consistent (KR `.fld` references KR `.lpt`, KR `.cam`, etc.).
The only USA-flavoured contents are the English text-bearing entries.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from afs import Afs, write_afs


# Extensions that hold English text in USA / Korean text in KR.
# Swap to USA for English UI/dialog/items.
# All identified by inspecting one sample of each ext and looking for English
# strings — see the SHIP.AFS extension reference notes.
USA_TEXT_EXTS = (
    ".cht",  # dialog text scripts ("What was the Captain thinking?")
    ".tui",  # UI labels ("Party", "Change members participating in battle.")
    ".itm",  # item names + descriptions ("Battered Sword")
    ".gft",  # gift dialog ("Can I really have it?")
    ".cha",  # character data ("Calintz")
    ".abi",  # ability definitions ("Rush Blade")
    ".sgi",  # combat-style descriptions ("A style that relies on super-quick strikes")
    ".cdg",  # talisman effects ("H:High Spirits")
    ".mdg",  # monster bestiary descriptions ("Geckra. This winged lizard-like...")
    ".dod",  # character titles ("Seiin Dojo Master")
    ".cls",  # class data
    ".ecd",  # event/cutscene dialog ("We need to get out of this cave.")
    ".att",  # attribute/stat data
    ".nod",  # area names ("Zekart's House", "Mountain Ruins")
    ".val",  # value data
    ".fds",  # friend/team dialog ("Calintz, let's go!")
    ".odd",  # side-quest dialog
    ".pod",  # talisman descriptions, 148 × 12,328 B (1.8 MB total)
)


def build(out_path: Path = ROOT / "build" / "kr_base" / "SHIP.AFS",
          usa_ship: Path = ROOT / "work" / "usa" / "SHIP.AFS",
          kr_ship: Path = ROOT / "work" / "kr" / "SHIP.AFS",
          verbose: bool = True) -> Path:
    usa = Afs.open(usa_ship); usa_n = usa.read_filename_toc()
    kr = Afs.open(kr_ship);  kr_n = kr.read_filename_toc()
    usa_idx = {n.lower(): i for i, n in enumerate(usa_n)}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, bytes]] = []
    swapped_from_usa = 0
    kept_kr = 0
    swapped_names: list[str] = []

    with kr_ship.open("rb") as fh_kr, usa_ship.open("rb") as fh_usa:
        for i, name in enumerate(kr_n):
            lname = name.lower()
            ext = "." + lname.rsplit(".", 1)[-1] if "." in lname else ""
            if ext in USA_TEXT_EXTS and lname in usa_idx:
                blob = usa.read_entry(usa_idx[lname], fh_usa)
                swapped_from_usa += 1
                swapped_names.append(name)
            else:
                blob = kr.read_entry(i, fh_kr)
                kept_kr += 1
            entries.append((name, blob))

    # IMPORTANT: pass through KR's TOC metadata as-is — the entry list and
    # order is unchanged from KR base, so KR's metadata (entry-count check at
    # TOC[0], redundant offset/size table) still validates.
    kr_meta = kr.read_toc_metadata()

    if verbose:
        print(f"  total entries (= KR's, unchanged):        {len(entries)}")
        print(f"  swapped to USA (English text bearers):    {swapped_from_usa}")
        print(f"  kept KR:                                  {kept_kr}")
        # show what we swapped
        from collections import Counter
        swapped_ext = Counter(("." + n.lower().rsplit(".",1)[-1]) for n in swapped_names)
        for e, c in swapped_ext.most_common():
            print(f"    swapped {c} {e} files")

    write_afs(out_path, entries, toc_metadata=kr_meta)
    if verbose:
        print(f"  wrote {out_path} ({out_path.stat().st_size:,} B)")
    return out_path


if __name__ == "__main__":
    build()
