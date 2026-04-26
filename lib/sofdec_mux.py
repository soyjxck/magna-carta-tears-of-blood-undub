"""Pure-Python CRI SofDec MPEG Program Stream muxer (v1).

Port of nebulas-star/SFD_Muxer (C, 2021) restricted to the case we need:
exactly 1 MPEG-1 video stream + 1 stereo CRI ADX audio stream (SFA-format),
SofDec version 1. Validated for byte-equivalence against the patched C build.

Inputs
------
- m1v: raw MPEG-1 elementary stream starting with `00 00 01 B3` (sequence
  header). frame_rate_code is byte 7 bit-low-nibble.
- sfa: CRI ADX audio file with `(c)CRI` watermark at offset 0x11A. Header:
    0x00..0x01  magic 0x80 0x00
    0x06        block_size (typically 0x12)
    0x07        channel_count
    0x08..0x0B  sample_rate (big-endian u32)

Output
------
- Sofdec MPEG-1 PS file laid out in 0x800-byte sectors:
    sector 0:  pack_head + audio system_header + 0xBE padding
    sector 1:  pack_head + video system_header + 0xBE padding
    sector 2:  pack_head + Sofdec stream-message PES (0xBF) + 0x00 padding
    sectors 3..N-1: interleaved video/audio PES packets, smallest DTS first
    final sector: program_end (0x000001B9) + 0x7FC bytes of 0xFF
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

SECTOR = 0x800

# Picture start code we scan video payloads for, to compute PTS/DTS.
PICTURE_START = b"\x00\x00\x01\x00"


# -- helpers --------------------------------------------------------------

def _pack_head(scr: int, mux_rate: int) -> bytes:
    """pack_start_code (4) + SCR (5) + mux_rate (3) = 12 bytes."""
    if scr > 0x1FFFFFFFF:
        raise ValueError(f"SCR overflow: {scr:#x}")
    a = (scr >> 29) | 0x21
    b = (scr >> 22) & 0xFF
    c = ((scr >> 14) & 0xFE) | 0x01
    d = (scr >> 7) & 0xFF
    e = ((scr << 1) & 0xFE) | 0x01
    scr_bytes = bytes([a, b, c, d, e])
    a = (mux_rate >> 15) | 0x80
    b = (mux_rate >> 7) & 0xFF
    c = ((mux_rate << 1) & 0xFE) | 0x01
    rate_bytes = bytes([a, b, c])
    return b"\x00\x00\x01\xba" + scr_bytes + rate_bytes


def _scr_for_block(block_num: int, mux_rate: int) -> int:
    """Replicates the C `SCR_made` formula. Note: C does INTEGER division
    `90001/50 = 1800` — Python `/` is float (1800.02) which drifts SCR by 1
    every ~5 sectors. Match C with `//`."""
    if block_num == 0:
        return 0
    a = (SECTOR * block_num) - 8
    c = (90001 // 50) * a
    d = c / mux_rate
    return int(d + 0.5)


def _system_header(mux_rate: int, video_bound: int, audio_bound: int,
                   audio_id_start_offset: int = 0) -> bytes:
    out = bytearray(b"\x00\x00\x01\xbb")
    header_length = 6 + 3 * (video_bound + audio_bound)
    out += struct.pack(">H", header_length)
    a = (mux_rate >> 15) | 0x80
    b = (mux_rate >> 7) & 0xFF
    c = ((mux_rate << 1) & 0xFE) | 0x01
    out += bytes([a, b, c])
    bound_a = (audio_bound << 2) | 0x02
    bound_b = video_bound | 0x20
    out += bytes([bound_a, bound_b, 0xFF])
    for i in range(audio_bound):
        out += bytes([0xC0 + i + audio_id_start_offset, 0xC0, 0x04])
    for i in range(video_bound):
        out += bytes([0xE0 + i, 0xE0, 0x2E])
    return bytes(out)


def _padding_stream(packet_length: int) -> bytes:
    """Stream 0xBE filler. Total emitted bytes = 6 + (packet_length - 1) + 1
    = packet_length + 6."""
    if packet_length < 1:
        raise ValueError("padding length must be >= 1")
    return (b"\x00\x00\x01\xbe"
            + struct.pack(">H", packet_length)
            + b"\x0f"
            + b"\xff" * (packet_length - 1))


def _pts_dts(mark: int, ts: int) -> bytes:
    """5-byte PTS or DTS marker. mark: 0x01=DTS, 0x02=PTS-only, 0x03=PTS-with-DTS."""
    a = (ts >> 29) | (mark << 4) | 0x01
    b = (ts >> 22) & 0xFF
    c = ((ts >> 14) & 0xFE) | 0x01
    d = (ts >> 7) & 0xFF
    e = ((ts << 1) & 0xFE) | 0x01
    return bytes([a, b, c, d, e])


def _std_buffer(scale: int, size: int) -> bytes:
    a = 0x40 | ((scale << 5) | (size >> 8))
    b = size & 0xFF
    return bytes([a, b])


# -- DTS_basic tables (90 kHz ticks per frame / packet) -------------------

_FRAME_RATE_DTS = {
    0x01: 3753.75 + 15,   # 23.976 fps
    0x02: 3750 + 15,      # 24
    0x03: 3600 + 15,      # 25
    0x04: 3003 + 15,      # 29.97
    0x05: 3000 + 15,      # 30
    0x06: 1800 + 15,      # 50
    0x07: 1501.5 + 15,    # 59.94
    0x08: 1500 + 15,      # 60
}


def _read_m1v_dts_basic(m1v: bytes) -> float:
    if m1v[0:4] != b"\x00\x00\x01\xb3":
        raise ValueError(f"not an MPEG-1 elementary stream: head={m1v[:8].hex()}")
    code = m1v[7] & 0x0F
    if code not in _FRAME_RATE_DTS:
        raise ValueError(f"unknown MPEG-1 frame_rate_code: {code:#x}")
    return _FRAME_RATE_DTS[code]


def _read_sfa_params(sfa: bytes) -> tuple[int, int]:
    """Return (sample_rate, channel_count)."""
    if sfa[:2] != b"\x80\x00":
        raise ValueError(f"not an ADX/SFA file: head={sfa[:8].hex()}")
    if sfa[0x11A:0x120] != b"(c)CRI":
        raise ValueError(
            f"missing (c)CRI watermark at 0x11A — got {sfa[0x11A:0x120]!r}"
        )
    channels = sfa[0x07]
    sample_rate = struct.unpack(">I", sfa[0x08:0x0C])[0]
    return sample_rate, channels


def _sfa_rate(sample_rate: int, channels: int) -> int:
    """Per-stream contribution to the mux_rate. Matches `sfa_rate_made` in C."""
    if channels == 2:
        return int(sample_rate * (1097 / 48000) + 0.5)
    if channels == 1 and sample_rate == 24000:
        return 0x142
    raise ValueError(
        f"unsupported SFA: rate={sample_rate} channels={channels}"
    )


# -- picture analysis -----------------------------------------------------

def _parse_picture(payload: bytes, off: int) -> tuple[int, int]:
    """Return (picture_coding_type, temporal_reference) for a picture header
    that begins at `off` in `payload` (i.e. payload[off:off+4] == 00 00 01 00)."""
    pct = (payload[off + 5] >> 3) & 0x07
    # `temporal_reference_read` in the C source uses `(i >> 6) | (j >> 6)`.
    # That's a 4-bit value that combines the two boundary bytes' top bits.
    tr = (payload[off + 4] >> 6) | (payload[off + 5] >> 6)
    return pct, tr


# -- the muxer ------------------------------------------------------------

@dataclass
class _Stream:
    kind: str               # "audio" | "video"
    data: bytes
    pos: int = 0
    dts_basic: float = 0.0
    dts_forecast: float = 0.0
    finished: bool = False
    # video-only state for picture-driven PTS/DTS
    pic_basic: int = 0      # cumulative GOP base
    pic_current: int = 0
    pic_biggest: int = 0


class SofdecMuxer:
    SOFDEC_VERSION = 1

    def __init__(self, m1v_path: Path | str, sfa_path: Path | str):
        self.video = _Stream(kind="video", data=Path(m1v_path).read_bytes())
        self.audio = _Stream(kind="audio", data=Path(sfa_path).read_bytes())
        self.video.dts_basic = _read_m1v_dts_basic(self.video.data)
        sample_rate, channels = _read_sfa_params(self.audio.data)
        self.audio.dts_basic = 322_560_000 / (sample_rate * channels)
        self.mux_rate = (
            1
            + 0x40F38                                 # one m1v
            + _sfa_rate(sample_rate, channels)        # one sfa
        )
        if self.mux_rate >= 0x3FFFFF:
            raise ValueError("mux_rate exceeds 22-bit MPEG-1 PS limit")

    def _audio_packet(self, scr_block: int, payload: bytes,
                      stream_id: int = 0xC0) -> bytes:
        # length field counts STD(2) + PTS(5) + payload bytes
        length = len(payload) + 7
        sector = bytearray()
        sector += _pack_head(_scr_for_block(scr_block, self.mux_rate), self.mux_rate)
        sector += b"\x00\x00\x01" + bytes([stream_id])
        sector += struct.pack(">H", length)
        sector += _std_buffer(0, 0x04)
        sector += _pts_dts(0x02, int(self.audio.dts_forecast))
        sector += payload
        # Sector-fill via 0xBE padding stream of length (0x7E1 - len(payload)).
        # _padding_stream emits packet_length + 6 bytes of output, so total
        # added to the sector = (0x7E1 - k) + 6 = 0x7E7 - k bytes, which makes
        # 12 + 13 + k + (0x7E7 - k) = 0x800. Verified.
        sector += _padding_stream(0x7E1 - len(payload))
        assert len(sector) == SECTOR, (len(sector), SECTOR)
        return bytes(sector)

    def _video_packet(self, scr_block: int, payload: bytes,
                      pts: int, dts: int, dts_type: int,
                      stream_id: int = 0xE0) -> bytes:
        # dts_type: 1=I/P (STD+PTS+DTS), 3=B (5 stuff + STD + PTS), 4=no-pic (9 stuff + STD + 0x0F)
        length = len(payload) + 0x0C  # 12 bytes of header after start+length
        sector = bytearray()
        sector += _pack_head(_scr_for_block(scr_block, self.mux_rate), self.mux_rate)
        sector += b"\x00\x00\x01" + bytes([stream_id])
        sector += struct.pack(">H", length)
        if dts_type == 1:
            sector += _std_buffer(1, 0x2E)
            sector += _pts_dts(0x03, pts)
            sector += _pts_dts(0x01, dts)
        elif dts_type == 3:
            sector += b"\xff" * 5
            sector += _std_buffer(1, 0x2E)
            sector += _pts_dts(0x02, pts)
        elif dts_type == 4:
            sector += b"\xff" * 9
            sector += _std_buffer(1, 0x2E)
            sector += b"\x0f"
        else:
            raise ValueError(f"bad dts_type {dts_type}")
        sector += payload
        # If the payload was short (last sector of stream), pad to 0x800 with
        # either a padding stream or 0xFF reserved bytes. Match C semantics.
        deficit = SECTOR - len(sector)
        if deficit:
            # C: if (0x7DC - k) > 0 pad with stream, else reserved 0xFF.
            # We use the same threshold against payload length k.
            k = len(payload)
            if 0x7DC - k > 0:
                sector += _padding_stream(0x7DC - k)
            else:
                sector += b"\xff" * (0x7E2 - k)
        assert len(sector) == SECTOR, (len(sector), SECTOR)
        return bytes(sector)

    def _opening_blocks(self) -> bytes:
        """Three opening sectors: audio sys-header, video sys-header, sofdec stream-message."""
        out = bytearray()
        # Sector 0: audio system header (1 audio stream)
        out += _pack_head(_scr_for_block(0, self.mux_rate), self.mux_rate)
        out += _system_header(self.mux_rate, video_bound=0, audio_bound=1)
        # padding to fill sector: padding_stream of length (0x7E2 - 3*1) = 0x7DF
        out += _padding_stream(0x07E2 - 3 * 1)
        assert len(out) == SECTOR
        # Sector 1: video system header (1 video stream)
        out += _pack_head(_scr_for_block(1, self.mux_rate), self.mux_rate)
        out += _system_header(self.mux_rate, video_bound=1, audio_bound=0)
        out += _padding_stream(0x07E2 - 3 * 1)
        assert len(out) == 2 * SECTOR
        # Sector 2: sofdec stream message (private stream 1 = 0xBF)
        out += _pack_head(_scr_for_block(2, self.mux_rate), self.mux_rate)
        out += _sofdec_stream_message(self.SOFDEC_VERSION)
        out += b"\x00" * 0x780  # sofdec_padding_block
        assert len(out) == 3 * SECTOR
        return bytes(out)

    def _closing_block(self) -> bytes:
        """0x000001B9 program-end + 0x7FC bytes 0xFF, padded to one sector."""
        return b"\x00\x00\x01\xb9" + b"\xff" * 0x7FC

    def write(self, out_path: Path | str) -> int:
        """Write the SFD to disk. Returns total bytes written."""
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as fh:
            fh.write(self._opening_blocks())
            scr_block = 3
            scr_block = self._main_loop(fh, scr_block)
            fh.write(self._closing_block())
        return out.stat().st_size

    def _main_loop(self, fh: BinaryIO, scr_block: int) -> int:
        """Interleave audio + video packets, smallest pending DTS first.

        For each iteration: pick the unfinished stream with smaller
        DTS_forecast, read its next chunk, emit a sector for it, advance DTS.
        Stop when both streams are finished.
        """
        a, v = self.audio, self.video
        while not (a.finished and v.finished):
            if v.finished or (not a.finished and a.dts_forecast <= v.dts_forecast):
                self._emit_audio(fh, a, scr_block)
            else:
                self._emit_video(fh, v, scr_block)
            scr_block += 1
        return scr_block

    def _emit_audio(self, fh: BinaryIO, s: _Stream, scr_block: int) -> None:
        chunk = s.data[s.pos : s.pos + 0x7E0]
        s.pos += len(chunk)
        if not chunk:
            s.finished = True
            return
        fh.write(self._audio_packet(scr_block, chunk))
        s.dts_forecast += s.dts_basic
        if len(chunk) < 0x7E0:
            s.finished = True

    def _emit_video(self, fh: BinaryIO, s: _Stream, scr_block: int) -> None:
        chunk = s.data[s.pos : s.pos + 0x7E2]
        s.pos += len(chunk)
        if not chunk:
            s.finished = True
            return
        # Find first picture_start_code in this chunk.
        first = chunk.find(PICTURE_START)
        if first < 0 or first >= 0x7DA:
            # No picture header in this slot — emit as continuation.
            fh.write(self._video_packet(
                scr_block=scr_block,
                payload=chunk,
                pts=0, dts=0, dts_type=4,
            ))
        else:
            pct, tr = _parse_picture(chunk, first)
            pts = int((s.pic_basic + tr) * s.dts_basic)
            dts = int(s.dts_forecast)
            dts_type = 3 if pct == 0x03 else 1   # B-frame else I/P
            fh.write(self._video_packet(
                scr_block=scr_block, payload=chunk,
                pts=pts, dts=dts, dts_type=dts_type,
            ))
            # Walk all picture_start_codes in this chunk to advance picture
            # counters and DTS_forecast (matches the C inner while-loop).
            cur = first
            while cur >= 0:
                _, tr_i = _parse_picture(chunk, cur)
                s.pic_current += 1
                if tr_i > s.pic_biggest:
                    s.pic_biggest = tr_i
                if tr_i == 0:
                    s.pic_basic += s.pic_biggest + 1
                    s.pic_current = s.pic_basic
                s.dts_forecast = s.dts_basic * s.pic_current
                cur = chunk.find(PICTURE_START, cur + 1)
        if len(chunk) < 0x7E2:
            s.finished = True


def _sofdec_stream_message(version: int) -> bytes:
    out = bytearray(b"\x00\x00\x01\xbf\x07\xee")
    if version == 2:
        out[5] = 0xEE  # length stays
        out += b"\x08"
    else:
        out += b"\x00"  # padding to keep header at 20 bytes total
    # The C source writes a fixed 20-byte header where bytes 6..19 are zeros.
    # We just produced bytes 0..6; need 14 more zero bytes to reach 20.
    out += b"\x00" * (20 - len(out))
    version_block = bytearray(b"SofdecStream            ")
    if version == 2:
        version_block[0x0C] = 0x32
    out += bytes(version_block)
    identity = bytearray([0x02, 0xFF, 0x00, 0x00, 0x20, 0x21, 0x07, 0x14])
    if version == 2:
        identity[1] = 0x02
        identity[2] = 0x02
        identity[3] = 0xFF
    out += bytes(identity)
    if version == 1:
        out += b"\x00" * 0x20
    out += b"SFD_Muxer Ver.0.24 by Nebulas   "  # 0x20 bytes muxer id (kept verbatim)
    if version == 2:
        out += b"\x00" * 0x20
    return bytes(out)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--m1v", type=Path, required=True)
    ap.add_argument("--sfa", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    n = SofdecMuxer(args.m1v, args.sfa).write(args.out)
    print(f"wrote {args.out} ({n:,} bytes)")
