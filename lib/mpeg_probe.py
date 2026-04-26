"""Walk an MPEG Program Stream and report packet structure.

Used to design the SofDec audio replacer: confirm that USA and KR SFDs have the
same number of PACK + audio + video packets, and that audio PES payload sizes line up.
"""
from __future__ import annotations

import argparse
import struct
from dataclasses import dataclass
from pathlib import Path


PACK_START = b"\x00\x00\x01\xba"
SYS_HEADER = b"\x00\x00\x01\xbb"
PROG_END = b"\x00\x00\x01\xb9"
START_PREFIX = b"\x00\x00\x01"


@dataclass
class Packet:
    kind: str   # "pack" | "system" | "pes" | "end"
    stream_id: int  # 0 for pack/system/end
    file_offset: int
    total_length: int   # bytes consumed in source file (incl. start code + length)
    payload_offset: int  # absolute offset where PES payload starts (only for "pes")
    payload_length: int  # payload size for "pes", else 0


def iter_packets(buf: bytes):
    n = len(buf)
    pos = 0
    while pos + 4 <= n:
        if buf[pos : pos + 4] == PACK_START:
            b4 = buf[pos + 4]
            if (b4 & 0xF0) == 0x20:  # MPEG-1 PACK: 12 bytes
                length = 12
            else:  # MPEG-2 PACK: 14 + stuffing
                stuffing = buf[pos + 13] & 0x07
                length = 14 + stuffing
            yield Packet("pack", 0, pos, length, 0, 0)
            pos += length
            continue
        if buf[pos : pos + 4] == SYS_HEADER:
            (header_length,) = struct.unpack(">H", buf[pos + 4 : pos + 6])
            length = 6 + header_length
            yield Packet("system", 0, pos, length, 0, 0)
            pos += length
            continue
        if buf[pos : pos + 4] == PROG_END:
            yield Packet("end", 0, pos, 4, 0, 0)
            pos += 4
            continue
        if buf[pos : pos + 3] == START_PREFIX:
            sid = buf[pos + 3]
            is_pes = (0xC0 <= sid <= 0xEF) or sid in (0xBD, 0xBE, 0xBF)
            if is_pes:
                (pes_len,) = struct.unpack(">H", buf[pos + 4 : pos + 6])
                total_length = 6 + pes_len
                # Padding stream: payload is filler — don't try to find a "real" payload.
                if sid == 0xBE:
                    yield Packet("pes", sid, pos, total_length, pos + 6, pes_len)
                    pos += total_length
                    continue
                # MPEG-1 PES header parsing.
                hdr = pos + 6
                # 0xFF stuffing (up to 16)
                while hdr < pos + total_length and buf[hdr] == 0xFF:
                    hdr += 1
                if hdr < pos + total_length and (buf[hdr] & 0xC0) == 0x40:
                    hdr += 2  # STD buffer scale + size
                if hdr < pos + total_length:
                    flag = buf[hdr] >> 4
                    if flag == 0x2:
                        hdr += 5  # PTS only
                    elif flag == 0x3:
                        hdr += 10  # PTS + DTS
                    elif flag == 0x0:
                        hdr += 1  # single 0x0F byte
                    else:
                        hdr += 1
                payload_offset = hdr
                payload_length = (pos + total_length) - hdr
                yield Packet("pes", sid, pos, total_length, payload_offset, payload_length)
                pos += total_length
                continue
        # No match — resync 1 byte at a time
        pos += 1


def summarise(path: Path):
    data = path.read_bytes()
    by_kind: dict[str, int] = {}
    by_stream: dict[int, int] = {}
    audio_payload_sizes: list[int] = []
    video_payload_sizes: list[int] = []
    for p in iter_packets(data):
        by_kind[p.kind] = by_kind.get(p.kind, 0) + 1
        if p.kind == "pes":
            by_stream[p.stream_id] = by_stream.get(p.stream_id, 0) + 1
            if p.stream_id == 0xC0:
                audio_payload_sizes.append(p.payload_length)
            elif p.stream_id == 0xE0:
                video_payload_sizes.append(p.payload_length)
    print(f"=== {path}  ({len(data):,} bytes) ===")
    print(f"  packet kinds: {by_kind}")
    print(f"  PES streams : {dict((hex(k), v) for k, v in by_stream.items())}")
    if audio_payload_sizes:
        amin = min(audio_payload_sizes)
        amax = max(audio_payload_sizes)
        atot = sum(audio_payload_sizes)
        print(f"  audio PESes : {len(audio_payload_sizes)} packets, payload total={atot:,}, min={amin}, max={amax}")
    if video_payload_sizes:
        vmin = min(video_payload_sizes)
        vmax = max(video_payload_sizes)
        vtot = sum(video_payload_sizes)
        print(f"  video PESes : {len(video_payload_sizes)} packets, payload total={vtot:,}, min={vmin}, max={vmax}")


def compare(usa: Path, kr: Path):
    usa_pkts = list(iter_packets(usa.read_bytes()))
    kr_pkts = list(iter_packets(kr.read_bytes()))
    usa_audio = [p for p in usa_pkts if p.kind == "pes" and p.stream_id == 0xC0]
    kr_audio = [p for p in kr_pkts if p.kind == "pes" and p.stream_id == 0xC0]
    print(f"\nCompare: USA audio packets={len(usa_audio)}  KR audio packets={len(kr_audio)}")
    print(f"  USA audio payload total={sum(p.payload_length for p in usa_audio):,}")
    print(f"  KR  audio payload total={sum(p.payload_length for p in kr_audio):,}")
    if len(usa_audio) == len(kr_audio):
        same = 0
        diff = 0
        for u, k in zip(usa_audio, kr_audio):
            if u.payload_length == k.payload_length:
                same += 1
            else:
                diff += 1
        print(f"  payload-size match: same={same}, diff={diff}")
        if diff:
            # show first 5 mismatches
            shown = 0
            for i, (u, k) in enumerate(zip(usa_audio, kr_audio)):
                if u.payload_length != k.payload_length:
                    print(f"    pkt#{i}: USA={u.payload_length} KR={k.payload_length}")
                    shown += 1
                    if shown >= 5:
                        break


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--compare", nargs=2, type=Path, metavar=("USA", "KR"))
    args = p.parse_args()
    for path in args.paths:
        summarise(path)
    if args.compare:
        compare(*args.compare)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
