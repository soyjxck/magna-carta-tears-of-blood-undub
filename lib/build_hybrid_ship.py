"""Build a hybrid SHIP.AFS for the wholesale-swap undub.

Strategy: USA SHIP.AFS as base (all USA-required assets present).
For voice-trigger / lipsync / battle-voice extensions, swap KR contents in
(so triggers reference KR audio). Add any KR-only entries the KR triggers
might reference (avoids missing-asset crashes during scene load).
Regenerate AFSShipFileIndex.idx so the manifest matches actual contents.

Output: build/hybrid/SHIP.AFS
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from afs import Afs, write_afs


# Extensions whose contents we want from KR (voice path):
#   .fld - field/scene scripts (where voice IDs are referenced)
#   .lpt - lipsync curves (matched to Korean audio)
#   .btv - battle voice trigger arrays
#   .emi - per-character voice/state machine? (KR has differing emi-vs-Emi)
#   .lvt - level voice tables (per-area ambient audio mappings)
KR_OVERRIDE_EXTS = (".fld", ".lpt", ".btv", ".emi", ".lvt")


def build(out_path: Path = ROOT / "build" / "hybrid" / "SHIP.AFS",
          usa_ship: Path = ROOT / "work" / "usa" / "SHIP.AFS",
          kr_ship: Path = ROOT / "work" / "kr" / "SHIP.AFS",
          verbose: bool = True) -> Path:
    usa = Afs.open(usa_ship); usa_n = usa.read_filename_toc()
    kr = Afs.open(kr_ship);  kr_n = kr.read_filename_toc()

    # Case-insensitive lookup so "00001234.Emi" matches "00001234.emi" — USA
    # and KR sometimes capitalize differently.
    usa_idx = {n.lower(): i for i, n in enumerate(usa_n)}
    kr_idx = {n.lower(): i for i, n in enumerate(kr_n)}

    # Plan: produce an ordered list of (name, blob) pairs.
    #
    # 1. For every USA entry, keep it — but if its lower-cased name appears in
    #    KR AND its extension is in KR_OVERRIDE_EXTS, use the KR blob.
    # 2. Append every KR-only entry afterwards.
    # 3. Replace AFSShipFileIndex.idx with a freshly-generated manifest.

    out_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, bytes]] = []
    overrides_kr = 0
    keeps_usa = 0
    additions_kr = 0

    with usa_ship.open("rb") as fh_usa, kr_ship.open("rb") as fh_kr:
        for i, name in enumerate(usa_n):
            lname = name.lower()
            ext = "." + lname.rsplit(".", 1)[-1] if "." in lname else ""
            if ext in KR_OVERRIDE_EXTS and lname in kr_idx:
                blob = kr.read_entry(kr_idx[lname], fh_kr)
                overrides_kr += 1
            else:
                blob = usa.read_entry(i, fh_usa)
                keeps_usa += 1
            entries.append((name, blob))
        # additions: KR-only entries (case-insensitive)
        for j, kn in enumerate(kr_n):
            if kn.lower() in usa_idx:
                continue
            blob = kr.read_entry(j, fh_kr)
            entries.append((kn, blob))
            additions_kr += 1

    # Regenerate the manifest. The format is plaintext: a leading
    # "AFSShipFileIndex.idx\r\n" then each filename on its own line ending
    # with \r\n. (Matches the original USA file structure we hex-dumped.)
    manifest_lines = ["AFSShipFileIndex.idx"]
    manifest_lines.extend(name for name, _ in entries if name != "AFSShipFileIndex.idx")
    manifest = ("\r\n".join(manifest_lines) + "\r\n").encode("ascii")
    # Place the rebuilt manifest at the head (first slot), as in the original.
    new_entries: list[tuple[str, bytes]] = []
    placed_manifest = False
    for name, blob in entries:
        if name == "AFSShipFileIndex.idx":
            new_entries.append((name, manifest))
            placed_manifest = True
        else:
            new_entries.append((name, blob))
    if not placed_manifest:
        new_entries.insert(0, ("AFSShipFileIndex.idx", manifest))

    if verbose:
        print(f"  USA-base entries:           {len(usa_n)}")
        print(f"  KR-overridden (voice exts): {overrides_kr}")
        print(f"  USA kept:                   {keeps_usa}")
        print(f"  KR-only additions:          {additions_kr}")
        print(f"  total entries:              {len(new_entries)}")
        print(f"  rebuilt manifest:           {len(manifest):,} B")

    write_afs(out_path, new_entries)
    if verbose:
        print(f"  wrote {out_path} ({out_path.stat().st_size:,} B)")
    return out_path


if __name__ == "__main__":
    build()
