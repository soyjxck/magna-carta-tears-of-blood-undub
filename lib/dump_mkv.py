"""Dump every USA + KR cutscene as an MKV for side-by-side reference.

Video is stream-copied (no re-encode); audio is decoded from CRI ADX and
re-encoded to FLAC (lossless) so it can play in standard MKV players.

Layout
------
  build/reference/usa/MOVIE18/<name>.mkv
  build/reference/usa/MOVIE99/<name>.mkv
  build/reference/kr/MOVIE18/<name>.mkv
  build/reference/kr/MOVIE99/<name>.mkv
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from ffmpeg_libass import find_or_build_ffmpeg


def dump_one(ffmpeg: Path, sfd: Path, mkv: Path) -> None:
    """Re-encode video to H.264 (CRF 23, fast preset) and audio to FLAC.
    Stream-copying the original MPEG-1 video into MKV produces truncated
    files because the SofDec PS irregularly sets PTS on some packets;
    re-encoding sidesteps all of that and the resulting MKV is portable."""
    mkv.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts",
            "-i", str(sfd),
            "-map", "0:v", "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-map", "0:a?", "-c:a", "flac",
            str(mkv),
        ],
        check=True,
    )


def dump_all(out_root: Path = ROOT / "build" / "reference",
             usa_root: Path = ROOT / "work" / "usa",
             kr_root: Path = ROOT / "work" / "kr",
             skip_existing: bool = True) -> int:
    ffmpeg = find_or_build_ffmpeg()
    pairs: list[tuple[str, Path, Path]] = []
    for region, root in (("usa", usa_root), ("kr", kr_root)):
        for sub in ("MOVIE18", "MOVIE99"):
            for sfd in sorted((root / sub).glob("*.SFD")):
                mkv = out_root / region / sub / (sfd.stem + ".mkv")
                pairs.append((region, sfd, mkv))

    total = len(pairs)
    done = 0
    skipped = 0
    print(f"dumping {total} reference MKVs ...")
    for i, (region, sfd, mkv) in enumerate(pairs, 1):
        if skip_existing and mkv.exists() and mkv.stat().st_size > 1024:
            skipped += 1
            continue
        try:
            dump_one(ffmpeg, sfd, mkv)
            done += 1
            print(f"  [{i:>2}/{total}] {region} {sfd.parent.name}/{sfd.name} -> {mkv.relative_to(ROOT)} ({mkv.stat().st_size:,} B)")
        except subprocess.CalledProcessError as e:
            print(f"  [{i:>2}/{total}] FAIL {region} {sfd.parent.name}/{sfd.name}: {e}")
    print(f"\ndone: {done} written, {skipped} skipped (already exist).")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="overwrite existing MKVs")
    args = ap.parse_args()
    return dump_all(skip_existing=not args.force)


if __name__ == "__main__":
    raise SystemExit(main())
