"""Compare two AFS archives by filename TOC + content fingerprint.

Reports:
  - filenames in both, only-USA, only-KR
  - for shared filenames: how many have identical bytes vs. differ (= candidate undub swaps)
"""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from afs import Afs


def hash_entry(afs: Afs, idx: int, fh, sample_bytes: int = 0) -> str:
    e = afs.entries[idx]
    fh.seek(e.offset)
    if sample_bytes and e.size > sample_bytes:
        # cheap fingerprint: head + tail + size — good enough to flag "same vs different"
        head = fh.read(sample_bytes // 2)
        fh.seek(e.offset + e.size - sample_bytes // 2)
        tail = fh.read(sample_bytes // 2)
        h = hashlib.sha1()
        h.update(head)
        h.update(tail)
        h.update(e.size.to_bytes(8, "little"))
        return h.hexdigest()
    data = fh.read(e.size)
    return hashlib.sha1(data).hexdigest()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("usa")
    p.add_argument("kr")
    p.add_argument("--full-hash", action="store_true", help="hash full content (slow)")
    p.add_argument("--sample", type=int, default=4096)
    args = p.parse_args()

    usa = Afs.open(args.usa)
    kr = Afs.open(args.kr)
    usa_names = usa.read_filename_toc() or []
    kr_names = kr.read_filename_toc() or []
    if not usa_names or not kr_names:
        print("missing filename TOC; cannot compare by name")
        return 2

    usa_idx = {n: i for i, n in enumerate(usa_names)}
    kr_idx = {n: i for i, n in enumerate(kr_names)}

    shared = set(usa_idx) & set(kr_idx)
    only_usa = set(usa_idx) - set(kr_idx)
    only_kr = set(kr_idx) - set(usa_idx)

    print(f"USA entries: {len(usa_names)}")
    print(f"KR  entries: {len(kr_names)}")
    print(f"shared names: {len(shared)}")
    print(f"only in USA : {len(only_usa)}")
    print(f"only in KR  : {len(only_kr)}")

    if only_usa:
        s = sorted(only_usa)
        print(f"  USA-only sample: {s[:6]} ... {s[-3:]}")
    if only_kr:
        s = sorted(only_kr)
        print(f"  KR-only  sample: {s[:6]} ... {s[-3:]}")

    same_size_same_bytes = 0
    same_size_diff_bytes = 0
    diff_size = 0
    sample_bytes = 0 if args.full_hash else args.sample
    with open(args.usa, "rb") as fu, open(args.kr, "rb") as fk:
        for name in shared:
            ui = usa_idx[name]
            ki = kr_idx[name]
            ue = usa.entries[ui]
            ke = kr.entries[ki]
            if ue.size != ke.size:
                diff_size += 1
                continue
            uh = hash_entry(usa, ui, fu, sample_bytes)
            kh = hash_entry(kr, ki, fk, sample_bytes)
            if uh == kh:
                same_size_same_bytes += 1
            else:
                same_size_diff_bytes += 1

    print()
    print("Among shared names:")
    print(f"  identical bytes (no swap needed): {same_size_same_bytes}")
    print(f"  same size but DIFFERENT bytes  : {same_size_diff_bytes}  <- pure VO swap candidates")
    print(f"  different size                 : {diff_size}  <- swap requires AFS rebuild")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
