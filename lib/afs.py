"""CRI AFS archive parser.

Format (little-endian throughout):
  0x00  4   magic "AFS\0"
  0x04  4   entry_count
  0x08  N*8 entries: (offset, size) pairs
  ...
  trailing metadata block (filename TOC) often referenced by the last entry

Sub-files are typically 0x800-byte (CD sector) aligned. Common sub-file types:
  - ADX  (CRI ADPCM audio, magic 0x80 0x00 ... at offset 0)
  - AHX  (CRI MPEG audio)
  - raw PCM, or game-specific containers
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


AFS_MAGIC = b"AFS\x00"


@dataclass(frozen=True)
class AfsEntry:
    index: int
    offset: int
    size: int

    @property
    def end(self) -> int:
        return self.offset + self.size


@dataclass
class Afs:
    path: Path
    entries: list[AfsEntry]
    toc_offset: int  # offset of the trailing filename TOC, 0 if absent
    toc_size: int

    @classmethod
    def open(cls, path: str | Path) -> "Afs":
        path = Path(path)
        with path.open("rb") as fh:
            magic = fh.read(4)
            if magic != AFS_MAGIC:
                raise ValueError(f"{path}: not an AFS file (magic={magic!r})")
            (count,) = struct.unpack("<I", fh.read(4))
            entries: list[AfsEntry] = []
            for i in range(count):
                off, size = struct.unpack("<II", fh.read(8))
                entries.append(AfsEntry(i, off, size))
            # The next 8 bytes after the entry table are usually (toc_offset, toc_size)
            # describing a trailing filename block. Some games omit this — treat as best-effort.
            tail = fh.read(8)
            if len(tail) == 8:
                toc_offset, toc_size = struct.unpack("<II", tail)
            else:
                toc_offset, toc_size = 0, 0
        return cls(path=path, entries=entries, toc_offset=toc_offset, toc_size=toc_size)

    def read_filename_toc(self) -> list[str] | None:
        """Read the optional trailing filename TOC. 32 bytes per entry: 0x00..0x1F filename
        (null-padded), then per-entry timestamp/size fields vary by game. We just grab names.
        """
        if not self.toc_offset or not self.toc_size:
            return None
        with self.path.open("rb") as fh:
            fh.seek(self.toc_offset)
            blob = fh.read(self.toc_size)
        # Try 48-byte stride (common: 32-byte name + 16-byte metadata)
        for stride in (48, 32):
            if self.toc_size % stride != 0:
                continue
            count_in_toc = self.toc_size // stride
            if count_in_toc != len(self.entries):
                continue
            names: list[str] = []
            for i in range(count_in_toc):
                raw = blob[i * stride : i * stride + 32]
                name = raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")
                names.append(name)
            return names
        return None

    def read_toc_metadata(self) -> bytes | None:
        """Read the per-entry 16-byte metadata trailer in the filename TOC.
        Returns a bytes blob of length 16 * entry_count, or None if the TOC
        format isn't the standard 48-byte stride. Used by `write_afs` to
        replicate the engine-required metadata when round-tripping or
        building a near-original archive."""
        if not self.toc_offset or not self.toc_size:
            return None
        if self.toc_size != 48 * len(self.entries):
            return None
        with self.path.open("rb") as fh:
            fh.seek(self.toc_offset)
            blob = fh.read(self.toc_size)
        out = bytearray()
        for i in range(len(self.entries)):
            out += blob[i * 48 + 32 : i * 48 + 48]
        return bytes(out)

    def read_entry(self, index: int, fh: BinaryIO | None = None) -> bytes:
        e = self.entries[index]
        if fh is None:
            with self.path.open("rb") as fh2:
                fh2.seek(e.offset)
                return fh2.read(e.size)
        fh.seek(e.offset)
        return fh.read(e.size)

    def sniff(self, index: int, n: int = 16) -> bytes:
        with self.path.open("rb") as fh:
            fh.seek(self.entries[index].offset)
            return fh.read(n)


SECTOR = 0x800  # AFS sub-files are 0x800-aligned on disc


def _pad_to(buf: bytearray, alignment: int = SECTOR) -> None:
    rem = (-len(buf)) % alignment
    if rem:
        buf.extend(b"\x00" * rem)


def write_afs(out_path: str | Path,
              entries: list[tuple[str, bytes]],
              toc_metadata: bytes | None = None) -> Path:
    """Write a CRI AFS archive.

    `entries` is a list of (filename, blob) pairs in the order they should
    appear in the archive. `toc_metadata` is an optional 16-byte-per-entry
    metadata block to append after each filename in the trailing TOC; if
    omitted we emit zero-filled metadata (which the engine accepts — the
    metadata holds timestamps that the player ignores at runtime).

    Layout (matches what the engine reads):
      0x00         "AFS\\0"
      0x04         u32 entry_count
      0x08 + 8*N   (offset, size) per entry
      ... after entry table:
                   u32 toc_offset, u32 toc_size  (back-reference to TOC)
      pad to 0x800 alignment
      ... per entry: blob padded up to 0x800
      ... then TOC: per entry, 32-byte filename + 16-byte metadata
    """
    out = Path(out_path)
    n = len(entries)

    # -- Build header (entry table + toc pointer placeholders) --
    header = bytearray()
    header += AFS_MAGIC
    header += struct.pack("<I", n)
    entry_table_offset = len(header)
    header += b"\x00" * (8 * n)              # entry (offset, size) placeholders
    toc_pointer_offset = len(header)
    header += b"\x00" * 8                    # (toc_offset, toc_size) placeholder

    _pad_to(header, SECTOR)

    # -- Place each blob, recording (offset, size) --
    body = bytearray(header)
    placements: list[tuple[int, int]] = []
    for _name, blob in entries:
        offset = len(body)
        body += blob
        placements.append((offset, len(blob)))
        _pad_to(body, SECTOR)

    # -- Build trailing filename TOC (48 bytes per entry) --
    toc_offset = len(body)
    toc = bytearray()
    for i, (name, _) in enumerate(entries):
        if isinstance(name, bytes):
            name_bytes = name
        else:
            name_bytes = name.encode("ascii", errors="replace")
        if len(name_bytes) > 32:
            name_bytes = name_bytes[:32]
        toc += name_bytes + b"\x00" * (32 - len(name_bytes))
        if toc_metadata is not None:
            md = toc_metadata[i * 16: (i + 1) * 16]
            if len(md) < 16:
                md = md + b"\x00" * (16 - len(md))
        else:
            md = b"\x00" * 16
        toc += md
    toc_size = len(toc)
    body += toc
    _pad_to(body, SECTOR)

    # -- Patch entry table + toc pointer back into header --
    for i, (off, sz) in enumerate(placements):
        struct.pack_into("<II", body, entry_table_offset + 8 * i, off, sz)
    struct.pack_into("<II", body, toc_pointer_offset, toc_offset, toc_size)

    out.write_bytes(bytes(body))
    return out


def classify(magic: bytes) -> str:
    """Best-effort classification from the first few bytes of a sub-file."""
    if len(magic) < 4:
        return "tiny"
    if magic[:2] == b"\x80\x00":
        return "ADX"  # CRI ADPCM
    if magic[:4] == b"AHX(":
        return "AHX"
    if magic[:4] == b"RIFF":
        return "RIFF/WAV"
    if magic[:4] == b"AFS\x00":
        return "AFS (nested)"
    if magic[:4] == b"\x00\x00\x01\xba":
        return "MPEG-PS/SFD"
    if magic[:4] == b"\x00\x00\x00\x00":
        return "zero/padding"
    return f"unknown({magic[:4].hex()})"


if __name__ == "__main__":
    import sys

    for arg in sys.argv[1:]:
        afs = Afs.open(arg)
        print(f"\n=== {arg} ===")
        print(f"entries: {len(afs.entries)}  toc@{afs.toc_offset:#x} size={afs.toc_size}")
        names = afs.read_filename_toc()
        # show the first 12 and last 4 entries
        sample = list(range(min(12, len(afs.entries)))) + list(
            range(max(0, len(afs.entries) - 4), len(afs.entries))
        )
        for i in sample:
            e = afs.entries[i]
            magic = afs.sniff(i)
            tag = classify(magic)
            label = f" name={names[i]!r}" if names else ""
            print(
                f"  [{i:5d}] off={e.offset:#011x} size={e.size:>10d}  {tag:<14}  "
                f"head={magic[:12].hex()}{label}"
            )
        # type histogram across full archive
        hist: dict[str, int] = {}
        with open(arg, "rb") as fh:
            for e in afs.entries:
                if e.size == 0:
                    hist["empty"] = hist.get("empty", 0) + 1
                    continue
                fh.seek(e.offset)
                tag = classify(fh.read(8))
                hist[tag] = hist.get(tag, 0) + 1
        print("  type histogram:")
        for k, v in sorted(hist.items(), key=lambda kv: -kv[1]):
            print(f"    {k:<20s} {v}")
