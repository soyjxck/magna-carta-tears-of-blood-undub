"""Re-encode the text content of USA .cht files for the KR-engine path.

`.cht` layout (Magna Carta):
  +0x00  4   u32 (1725 in 00012047.cht — possibly "max_records" or similar)
  +0x04  4   u32 record_count
  +0x08  4   u32 (5 — flag? version?)
  +0x0C  4   u32 0 — reserved
  ----- record 0 starts here -----
  +0x10  4   u32 sequence index
  +0x14  4   u32 0
  +0x18  4   u32 speaker_id
  +0x1C  4   u32 marker  (consistently 0x04 for first record's text marker)
  +0x20      text bytes (USA: ASCII; KR: CP949 / EUC-KR)
  +0x20+L    null padding to 532 bytes
  ----- record 1 at +0x10 + 532 -----

This module recodes USA's ASCII text to UTF-16 LE so each character takes
2 bytes — testing the hypothesis that KR engine reads .cht text in 16-bit
mode. We don't change the record header at all.

Usage:
    cht_recoder.recode_file(usa_cht_bytes) -> bytes
"""
from __future__ import annotations

import struct
from pathlib import Path


FILE_HEADER_SIZE = 0x10        # 16 bytes before first record
RECORD_SIZE = 532
TEXT_OFFSET_IN_RECORD = 0x10   # text starts at byte 16 of each 532-byte record (after the 16-byte sub-header)


def _ascii_to_cp949_fullwidth(s: str) -> bytes:
    """Map ASCII chars to CP949 (EUC-KR) fullwidth equivalents in the
    GraphicCharacters block.  ASCII space U+0020 -> CP949 0xA1A1, then
    !..~ map to 0xA1A2..0xA3FE in order. Korean fonts include fullwidth
    Latin glyphs in this range so this trick renders English readable
    via the engine's normal dual-byte lookup path."""
    out = bytearray()
    for ch in s:
        cp = ord(ch)
        if cp == 0x20:
            out += b"\xA1\xA1"          # fullwidth space
        elif 0x21 <= cp <= 0x7E:
            # CP949 fullwidth ASCII range: 0xA3A1 + (cp - 0x21) for !..~
            # (gives !=0xA3A1, A=0xA3C1, a=0xA3E1, ~=0xA3FE)
            wide = 0xA3A1 + (cp - 0x21)
            out.append((wide >> 8) & 0xFF)
            out.append(wide & 0xFF)
        elif cp == 0x0A:
            out += b"\x0A"               # newline passthrough
        else:
            # leave anything else alone
            out += ch.encode("latin-1", errors="ignore")
    return bytes(out)


def _recode_record(record: bytes, encoding: str = "utf-16-le") -> bytes:
    """Return a new 532-byte record where the text portion has been recoded
    from ASCII to `encoding`. Sub-header is preserved verbatim.

    Special encoding `cp949-fullwidth` maps ASCII to CP949 fullwidth Latin
    glyphs (so a Korean font's Hangul-side dual-byte lookup renders English).
    """
    assert len(record) == RECORD_SIZE
    head = record[:TEXT_OFFSET_IN_RECORD]
    text_region = record[TEXT_OFFSET_IN_RECORD:]
    end = text_region.find(b"\x00")
    if end < 0:
        end = len(text_region)
    raw_text = text_region[:end]
    try:
        decoded = raw_text.decode("ascii")
    except UnicodeDecodeError:
        return record
    if encoding == "cp949-fullwidth":
        encoded = _ascii_to_cp949_fullwidth(decoded) + b"\x00"
    else:
        encoded = decoded.encode(encoding) + b"\x00\x00"
    if len(encoded) > len(text_region):
        # truncate
        if encoding == "cp949-fullwidth":
            max_chars = (len(text_region) - 1) // 2  # 2 bytes per char + null
            encoded = _ascii_to_cp949_fullwidth(decoded[:max_chars]) + b"\x00"
        else:
            max_chars = (len(text_region) - 2) // 2
            encoded = decoded[:max_chars].encode(encoding) + b"\x00\x00"
    padded = encoded + b"\x00" * (len(text_region) - len(encoded))
    return head + padded


def recode_file(blob: bytes, encoding: str = "utf-16-le") -> bytes:
    """Recode every record's text in a `.cht` file."""
    if len(blob) < FILE_HEADER_SIZE + RECORD_SIZE:
        return blob
    header = blob[:FILE_HEADER_SIZE]
    body = blob[FILE_HEADER_SIZE:]
    out = bytearray(header)
    n_records = len(body) // RECORD_SIZE
    for i in range(n_records):
        rec = body[i * RECORD_SIZE : (i + 1) * RECORD_SIZE]
        out += _recode_record(rec, encoding)
    # Append any tail bytes (shouldn't be any, but be safe)
    tail = len(body) - n_records * RECORD_SIZE
    if tail:
        out += body[n_records * RECORD_SIZE :]
    return bytes(out)


if __name__ == "__main__":
    import sys
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    enc = sys.argv[3] if len(sys.argv) > 3 else "utf-16-le"
    data = src.read_bytes()
    new = recode_file(data, encoding=enc)
    dst.write_bytes(new)
    print(f"recoded {src.name}  size {len(data)} -> {len(new)} (encoding={enc})")
