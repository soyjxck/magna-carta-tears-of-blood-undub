"""Classify every USA SFD into one of four tiers vs. its KR counterpart.

  Tier 1 — byte-identical (no swap needed; silent/logo cutscenes)
  Tier 2 — perfect packet alignment (Tier-2 byte-for-byte swap works)
  Tier 3 — same duration, different packetization (needs real demux/remux)
  Tier 4 — different cutscene durations (cannot losslessly undub)

Outputs one CSV-ish line per SFD plus a summary footer.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mpeg_probe import iter_packets


AUDIO_STREAM_ID = 0xC0
DURATION_TOLERANCE_S = 0.5  # frame-level slop


def ffprobe_duration(path: Path) -> float:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-hide_banner",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ]
    )
    return float(json.loads(out)["format"]["duration"])


def classify_pair(usa: Path, kr: Path) -> dict:
    info: dict = {"usa": str(usa), "kr": str(kr)}
    if usa.stat().st_size == kr.stat().st_size and usa.read_bytes() == kr.read_bytes():
        info["tier"] = 1
        return info
    ud = usa.read_bytes()
    kd = kr.read_bytes()
    ua = [p for p in iter_packets(ud) if p.kind == "pes" and p.stream_id == AUDIO_STREAM_ID]
    ka = [p for p in iter_packets(kd) if p.kind == "pes" and p.stream_id == AUDIO_STREAM_ID]
    info["usa_audio_packets"] = len(ua)
    info["kr_audio_packets"] = len(ka)
    info["usa_audio_bytes"] = sum(p.payload_length for p in ua)
    info["kr_audio_bytes"] = sum(p.payload_length for p in ka)
    if len(ua) == len(ka) and all(u.payload_length == k.payload_length for u, k in zip(ua, ka)):
        info["tier"] = 2
        return info
    udur = ffprobe_duration(usa)
    kdur = ffprobe_duration(kr)
    info["usa_duration"] = udur
    info["kr_duration"] = kdur
    if abs(udur - kdur) <= DURATION_TOLERANCE_S:
        info["tier"] = 3
    else:
        info["tier"] = 4
    return info


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--usa-root", default="work/usa")
    ap.add_argument("--kr-root", default="work/kr")
    ap.add_argument("--json", type=Path, help="write detailed JSON report")
    args = ap.parse_args()

    rows: list[dict] = []
    by_tier: dict[int, list[str]] = {1: [], 2: [], 3: [], 4: []}
    for sub in ("MOVIE18", "MOVIE99"):
        usa_dir = Path(args.usa_root) / sub
        kr_dir = Path(args.kr_root) / sub
        if not usa_dir.is_dir() or not kr_dir.is_dir():
            continue
        for fn in sorted(os.listdir(usa_dir)):
            if not fn.upper().endswith(".SFD"):
                continue
            u = usa_dir / fn
            k = kr_dir / fn
            if not k.exists():
                print(f"  {sub}/{fn}: KR missing")
                continue
            info = classify_pair(u, k)
            info["name"] = f"{sub}/{fn}"
            rows.append(info)
            by_tier[info["tier"]].append(info["name"])
            print(f"  tier {info['tier']}  {sub}/{fn}")

    print()
    for t in (1, 2, 3, 4):
        print(f"Tier {t}: {len(by_tier[t])}")
        for name in by_tier[t]:
            print(f"    {name}")
    if args.json:
        args.json.write_text(json.dumps({"rows": rows, "by_tier": by_tier}, indent=2))
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
