"""Minimal-change SHIP.AFS: USA's exact entry list (count, names, order),
with the BLOBS of voice-extension entries replaced by KR's content where the
filename exists in both regions.

This avoids:
  - Adding KR-only entries (so total entry count stays at USA's 13921)
  - Rewriting AFSShipFileIndex.idx (USA filename list still matches)
  - Touching AFSINFO.INI (engine's preallocated buffer cap unchanged)

What it does:
  - For each USA entry, if its lower-cased name is in KR AND its extension
    is one of the voice-trigger extensions (.fld/.lpt/.btv/.emi/.lvt),
    write KR's bytes at that position (sub-file may grow/shrink — that's fine,
    write_afs handles arbitrary sizes).
  - Otherwise keep USA bytes verbatim.

Result: a SHIP.AFS that the USA engine should parse successfully (same
filename list it expects, same total entry count). The only difference is
the *contents* of voice trigger files now reference KR voice IDs.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from afs import Afs, write_afs

KR_OVERRIDE_EXTS = (".fld", ".lpt", ".btv", ".emi", ".lvt")


def build(out_path: Path = ROOT / "build" / "minimal" / "SHIP.AFS",
          usa_ship: Path = ROOT / "work" / "usa" / "SHIP.AFS",
          kr_ship: Path = ROOT / "work" / "kr" / "SHIP.AFS",
          verbose: bool = True) -> Path:
    usa = Afs.open(usa_ship); usa_n = usa.read_filename_toc()
    kr = Afs.open(kr_ship);  kr_n = kr.read_filename_toc()
    kr_idx = {n.lower(): i for i, n in enumerate(kr_n)}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, bytes]] = []
    overrides = 0
    keeps = 0
    with usa_ship.open("rb") as fh_usa, kr_ship.open("rb") as fh_kr:
        for i, name in enumerate(usa_n):
            lname = name.lower()
            ext = "." + lname.rsplit(".", 1)[-1] if "." in lname else ""
            if ext in KR_OVERRIDE_EXTS and lname in kr_idx:
                blob = kr.read_entry(kr_idx[lname], fh_kr)
                overrides += 1
            else:
                blob = usa.read_entry(i, fh_usa)
                keeps += 1
            entries.append((name, blob))

    if verbose:
        print(f"  total entries: {len(entries)} (= USA's {len(usa_n)}, unchanged)")
        print(f"  KR-overridden (voice-related): {overrides}")
        print(f"  USA kept verbatim:             {keeps}")

    # Pass through USA's original TOC metadata so the engine's parse-time
    # entry-count check (in TOC[0] metadata) and any other validation it does
    # against the metadata table sees the values it expects.
    usa_meta = usa.read_toc_metadata()
    write_afs(out_path, entries, toc_metadata=usa_meta)
    if verbose:
        print(f"  wrote {out_path} ({out_path.stat().st_size:,} B)")
    return out_path


if __name__ == "__main__":
    build()
