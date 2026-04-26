"""Hybrid LINEAR.AFS: USA base (gives us USA-flavored content for the 124
differing-size shared .lin files), with the 74 KR-only .lin files appended
so that KR `.fld` references can resolve.

Drops the 74 USA-only .lin files (the KR `.fld` files we use shouldn't
reference them). Net entry count stays close to USA's 4099 (= 4025 shared
+ 74 KR-only).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from afs import Afs, write_afs


def build(out_path: Path = ROOT / "build" / "hybrid" / "LINEAR.AFS",
          usa_linear: Path = ROOT / "work" / "usa" / "LINEAR.AFS",
          kr_linear: Path = ROOT / "work" / "kr" / "LINEAR.AFS",
          verbose: bool = True) -> Path:
    usa = Afs.open(usa_linear); usa_n = usa.read_filename_toc()
    kr = Afs.open(kr_linear);  kr_n = kr.read_filename_toc()
    usa_idx = {n.lower(): i for i, n in enumerate(usa_n)}
    kr_idx = {n.lower(): i for i, n in enumerate(kr_n)}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, bytes]] = []
    kept_usa = 0
    skipped_usa_only = 0

    with usa_linear.open("rb") as fu, kr_linear.open("rb") as fk:
        # Walk USA's entry list. Keep entries that are also in KR (shared);
        # drop USA-only entries (KR `.fld` shouldn't reference them).
        for i, name in enumerate(usa_n):
            ln = name.lower()
            if ln in kr_idx:
                blob = usa.read_entry(i, fu)
                entries.append((name, blob))
                kept_usa += 1
            else:
                skipped_usa_only += 1

        # Append KR-only entries (filenames not in USA).
        kr_only_added = 0
        for i, name in enumerate(kr_n):
            if name.lower() in usa_idx:
                continue
            blob = kr.read_entry(i, fk)
            entries.append((name, blob))
            kr_only_added += 1

    if verbose:
        print(f"  shared USA entries kept:  {kept_usa}")
        print(f"  USA-only dropped:         {skipped_usa_only}")
        print(f"  KR-only appended:         {kr_only_added}")
        print(f"  total entries:            {len(entries)}")

    # Build TOC metadata: use USA's existing metadata for USA-base entries
    # (with TOC[0] entry-count patched), zero-fill for KR-only adds.
    import struct
    usa_meta = usa.read_toc_metadata() or b""
    total_n = len(entries)
    meta = bytearray(16 * total_n)
    # Copy USA's metadata for entries that survived (kept_usa of them)
    # — but we dropped some USA entries from the middle, so we need to map
    # carefully. Rebuild metadata based on our entry order.
    # For now: keep TOC[0] entry-count = total_n; zero rest.
    struct.pack_into("<I", meta, 12, total_n)

    write_afs(out_path, entries, toc_metadata=bytes(meta))
    if verbose:
        print(f"  wrote {out_path} ({out_path.stat().st_size:,} B)")
    return out_path


if __name__ == "__main__":
    build()
