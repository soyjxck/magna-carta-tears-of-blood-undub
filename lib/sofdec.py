"""SofDec MPEG-PS audio replacement for Magna Carta cutscenes.

Strategy A — `swap_audio_packets`:
    Both SFDs must have the same audio PES packet count, with each pair of
    USA[i] / KR[i] payloads being the same byte length. We then replace USA's
    audio PES payloads with KR's bytes, leaving every byte of video + PACK
    header + SCR/PTS timing untouched. Output file is the same size as USA.

This works for SFDs we've classified Tier-2 (perfect packet alignment).
For Tier-3 (same duration, different packetization) and Tier-4 (different
scene durations) we currently fail loudly — those need a real SofDec muxer
which we'll build separately.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mpeg_probe import iter_packets, Packet


AUDIO_STREAM_ID = 0xC0


@dataclass
class SwapPlan:
    audio_count: int
    audio_payload_total: int
    matches: bool
    first_mismatch: tuple[int, int, int] | None  # (idx, usa_len, kr_len)


def plan(usa_path: Path, kr_path: Path) -> SwapPlan:
    usa = usa_path.read_bytes()
    kr = kr_path.read_bytes()
    ua = [p for p in iter_packets(usa) if p.kind == "pes" and p.stream_id == AUDIO_STREAM_ID]
    ka = [p for p in iter_packets(kr) if p.kind == "pes" and p.stream_id == AUDIO_STREAM_ID]
    if len(ua) != len(ka):
        return SwapPlan(len(ua), sum(p.payload_length for p in ua), False, (-1, len(ua), len(ka)))
    first = None
    for i, (u, k) in enumerate(zip(ua, ka)):
        if u.payload_length != k.payload_length:
            first = (i, u.payload_length, k.payload_length)
            break
    return SwapPlan(
        audio_count=len(ua),
        audio_payload_total=sum(p.payload_length for p in ua),
        matches=first is None,
        first_mismatch=first,
    )


def swap_audio_packets(usa_path: Path, kr_path: Path, out_path: Path) -> SwapPlan:
    """Tier-2 swap: USA video + KR audio, byte-for-byte payload replacement.

    Raises ValueError if packet counts or any payload size differs.
    """
    usa = bytearray(usa_path.read_bytes())
    kr = kr_path.read_bytes()
    usa_audio: list[Packet] = [
        p for p in iter_packets(bytes(usa)) if p.kind == "pes" and p.stream_id == AUDIO_STREAM_ID
    ]
    kr_audio: list[Packet] = [
        p for p in iter_packets(kr) if p.kind == "pes" and p.stream_id == AUDIO_STREAM_ID
    ]
    if len(usa_audio) != len(kr_audio):
        raise ValueError(
            f"audio packet count mismatch: USA={len(usa_audio)} KR={len(kr_audio)}"
        )
    for i, (u, k) in enumerate(zip(usa_audio, kr_audio)):
        if u.payload_length != k.payload_length:
            raise ValueError(
                f"audio packet[{i}] payload size mismatch: USA={u.payload_length} KR={k.payload_length}"
            )
    for u, k in zip(usa_audio, kr_audio):
        usa[u.payload_offset : u.payload_offset + u.payload_length] = kr[
            k.payload_offset : k.payload_offset + k.payload_length
        ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(usa))
    return SwapPlan(len(usa_audio), sum(p.payload_length for p in usa_audio), True, None)


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("usa", type=Path)
    ap.add_argument("kr", type=Path)
    ap.add_argument("--out", type=Path, required=False)
    ap.add_argument("--plan-only", action="store_true")
    args = ap.parse_args()
    if args.plan_only or not args.out:
        p = plan(args.usa, args.kr)
        print(f"audio packets : {p.audio_count}")
        print(f"audio payload : {p.audio_payload_total:,} bytes")
        print(f"swap-eligible : {p.matches}")
        if p.first_mismatch:
            i, u, k = p.first_mismatch
            print(f"first mismatch: idx={i} USA={u} KR={k}")
        sys.exit(0 if p.matches else 1)
    res = swap_audio_packets(args.usa, args.kr, args.out)
    print(
        f"OK: swapped {res.audio_count} audio packets "
        f"({res.audio_payload_total:,} bytes) -> {args.out}"
    )
