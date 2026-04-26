"""Build USA -> KR voice ID mapping by pairing entries inside .btv files.

A `.btv` (battle voice trigger) file in SHIP.AFS contains one character's
voice array: 12-byte header followed by N u32 LE voice IDs at fixed positions
across both regions. Pairing position-i USA[i] with KR[i] yields a direct map
from English voice IDs to Korean voice IDs.

We only emit a mapping when:
  - both regions hold the same .btv filename, and
  - both have the same record count, and
  - both ID values are valid (not 0xFFFFFFFF sentinel).
"""
from __future__ import annotations

import re
import struct
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from afs import Afs

SENTINEL = 0xFFFFFFFF


def music_voice_ids(afs_path: Path) -> set[int]:
    a = Afs.open(afs_path)
    return {
        int(m.group(1))
        for n in a.read_filename_toc()
        for m in [re.match(r"(\d+)\.adx$", n)]
        if m
    }


def parse_btv(blob: bytes) -> list[int]:
    """Return the voice ID array (skipping the 3-u32 header)."""
    if len(blob) < 12:
        return []
    count = struct.unpack_from("<I", blob, 0)[0]
    body = blob[12:12 + 4 * count]
    if len(body) < 4 * count:
        return []
    return list(struct.unpack(f"<{count}I", body))


def build_mapping_from_fld(usa_ship: Path, kr_ship: Path,
                           usa_voice: set[int], kr_voice: set[int]
                           ) -> tuple[dict[int, int], dict[str, int]]:
    """For each shared .fld scene script, extract language-specific voice IDs
    in order of appearance and pair USA[i] with KR[i].

    Filters to only USA-only and KR-only IDs (i.e. language-specific VO),
    skipping shared SFX which are the same in both regions and only add noise.
    Skips .fld files where the counts don't match (the scene was reworked).
    """
    usa = Afs.open(usa_ship); usa_n = usa.read_filename_toc()
    kr = Afs.open(kr_ship);  kr_n = kr.read_filename_toc()
    ui = {n.lower(): i for i, n in enumerate(usa_n)}
    ki = {n.lower(): i for i, n in enumerate(kr_n)}
    usa_only = usa_voice - kr_voice
    kr_only = kr_voice - usa_voice

    def lang_specific_ids_in_order(blob: bytes, allow: set[int]) -> list[int]:
        out = []
        for off in range(0, len(blob) - 4, 4):
            v = struct.unpack_from("<I", blob, off)[0]
            if v in allow:
                out.append(v)
        return out

    mapping: dict[int, int] = {}
    conflicts = Counter()
    fld_files = sorted([n.lower() for n in usa_n if n.lower().endswith(".fld")])
    paired = 0
    matched_count_files = 0
    skipped = 0
    for name in fld_files:
        if name not in ki:
            continue
        ud = usa.read_entry(ui[name])
        kd = kr.read_entry(ki[name])
        u_arr = lang_specific_ids_in_order(ud, usa_only)
        k_arr = lang_specific_ids_in_order(kd, kr_only)
        if len(u_arr) == 0 or len(k_arr) == 0:
            continue
        if len(u_arr) != len(k_arr):
            skipped += 1
            continue
        matched_count_files += 1
        for u, k in zip(u_arr, k_arr):
            paired += 1
            if u in mapping and mapping[u] != k:
                conflicts[u] += 1
            mapping[u] = k
    return mapping, {
        "fld_files_total": len(fld_files),
        "fld_files_count_matched": matched_count_files,
        "fld_files_count_mismatch_skipped": skipped,
        "pair_writes": paired,
        "unique_usa_ids": len(mapping),
        "conflicting_usa_ids": len(conflicts),
    }


def build_mapping_from_btv(usa_ship: Path, kr_ship: Path,
                           usa_voice: set[int], kr_voice: set[int]
                           ) -> tuple[dict[int, int], dict[str, int]]:
    """Walk every .btv that exists in both regions, pair index-by-index, return
    {usa_voice_id: kr_voice_id} plus stats."""
    usa = Afs.open(usa_ship); usa_n = usa.read_filename_toc()
    kr = Afs.open(kr_ship);  kr_n = kr.read_filename_toc()
    ui = {n.lower(): i for i, n in enumerate(usa_n)}
    ki = {n.lower(): i for i, n in enumerate(kr_n)}

    mapping: dict[int, int] = {}
    conflicts = Counter()
    btv_files = sorted([n.lower() for n in usa_n if n.lower().endswith(".btv")])
    paired = 0
    skipped_filename = 0
    skipped_count = 0
    for name in btv_files:
        if name not in ki:
            skipped_filename += 1
            continue
        ud = usa.read_entry(ui[name])
        kd = kr.read_entry(ki[name])
        u_arr = parse_btv(ud)
        k_arr = parse_btv(kd)
        if len(u_arr) != len(k_arr) or not u_arr:
            skipped_count += 1
            continue
        for u, k in zip(u_arr, k_arr):
            if u == SENTINEL or k == SENTINEL:
                continue
            if u not in usa_voice or k not in kr_voice:
                continue
            paired += 1
            if u in mapping and mapping[u] != k:
                conflicts[u] += 1
            mapping[u] = k

    stats = {
        "btv_files_total": len(btv_files),
        "btv_files_skipped_filename_mismatch": skipped_filename,
        "btv_files_skipped_count_mismatch": skipped_count,
        "pair_writes": paired,
        "unique_usa_ids": len(mapping),
        "conflicting_usa_ids": len(conflicts),
    }
    return mapping, stats


def main() -> int:
    import json, argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=ROOT / "build" / "voice_mapping.json")
    args = ap.parse_args()

    usa_voice = music_voice_ids(ROOT / "work/usa/MUSIC.AFS")
    kr_voice = music_voice_ids(ROOT / "work/kr/MUSIC.AFS")
    print(f"USA voice IDs: {len(usa_voice)}, KR voice IDs: {len(kr_voice)}")

    btv_map, btv_stats = build_mapping_from_btv(
        ROOT / "work/usa/SHIP.AFS",
        ROOT / "work/kr/SHIP.AFS",
        usa_voice, kr_voice,
    )
    fld_map, fld_stats = build_mapping_from_fld(
        ROOT / "work/usa/SHIP.AFS",
        ROOT / "work/kr/SHIP.AFS",
        usa_voice, kr_voice,
    )
    print("=== .btv ===")
    for k, v in btv_stats.items(): print(f"  {k}: {v}")
    print("=== .fld ===")
    for k, v in fld_stats.items(): print(f"  {k}: {v}")

    # merge: .btv first (most reliable), fld fills gaps
    mapping = {**fld_map, **btv_map}  # btv wins on conflicts
    coverage = 100.0 * len(mapping) / len(usa_voice)
    print(f"\nCOMBINED: {len(mapping)} / {len(usa_voice)} USA voice IDs ({coverage:.1f}%)")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({str(k): v for k, v in sorted(mapping.items())}, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
