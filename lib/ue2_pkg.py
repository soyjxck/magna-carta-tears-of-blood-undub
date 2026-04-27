"""Minimal Unreal Engine 2 package parser — just enough to enumerate
exports by class so we can find Texture / Font objects inside `.u` /
`.utx` packages.

UE2 package layout (we only need read paths):
  +0x00   u32 magic = 0xC1832A9E
  +0x04   u16 file_version, u16 license_version  (-> 'package_ver' u32 read)
  +0x08   u32 package_flags
  +0x0C   u32 name_count
  +0x10   u32 name_offset
  +0x14   u32 export_count
  +0x18   u32 export_offset
  +0x1C   u32 import_count
  +0x20   u32 import_offset
  ...
  Name table:    each entry = u8 length + ASCII string + 0x00 + u32 flags
  Import table:  ClassPackage idx | ClassName idx | OuterIndex i32 | ObjectName idx
                 (all "compact index" except OuterIndex which is signed u32)
  Export table:  ClassIndex (compact, signed) | SuperIndex (compact, signed)
                 | PackageIndex (u32) | ObjectName (compact) | ObjectFlags u32
                 | SerialSize (compact) | SerialOffset (compact, only if size>0)

Compact-index encoding (variable 1-5 bytes):
  byte 0: bit 7 = sign, bit 6 = more, bits 0-5 = low 6 bits of value
  byte 1+: bit 7 = more,                bits 0-6 = next 7 bits of value
The decoded value is signed (sign bit is in byte 0 bit 7).
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path


MAGIC = 0x9E2A83C1   # file bytes c1 83 2a 9e read as LE u32


@dataclass
class Import:
    class_package: int
    class_name: int
    outer_index: int
    object_name: int


@dataclass
class Export:
    class_index: int      # signed compact: <0 = import idx (-1-based), >0 = export idx (1-based), 0 = native class
    super_index: int
    package_index: int
    object_name: int
    object_flags: int
    serial_size: int
    serial_offset: int


@dataclass
class Package:
    path: Path
    data: bytes = field(repr=False)
    package_ver: int = 0
    package_flags: int = 0
    names: list[str] = field(default_factory=list)
    imports: list[Import] = field(default_factory=list)
    exports: list[Export] = field(default_factory=list)


def _read_compact_index(buf: bytes, pos: int) -> tuple[int, int]:
    """Returns (value, new_position). Value is signed."""
    b = buf[pos]; pos += 1
    sign = -1 if (b & 0x80) else 1
    val = b & 0x3F
    if b & 0x40:  # has more bytes
        shift = 6
        while True:
            c = buf[pos]; pos += 1
            val |= (c & 0x7F) << shift
            shift += 7
            if not (c & 0x80):
                break
    return sign * val, pos


def open_pkg(path: str | Path) -> Package:
    p = Path(path)
    data = p.read_bytes()
    if struct.unpack_from("<I", data, 0)[0] != MAGIC:
        raise ValueError(f"{p}: not a UE2 package (magic={data[:4].hex()})")
    pkg = Package(path=p, data=data)
    pkg.package_ver = struct.unpack_from("<I", data, 4)[0]
    pkg.package_flags = struct.unpack_from("<I", data, 8)[0]
    name_count = struct.unpack_from("<I", data, 12)[0]
    name_offset = struct.unpack_from("<I", data, 16)[0]
    export_count = struct.unpack_from("<I", data, 20)[0]
    export_offset = struct.unpack_from("<I", data, 24)[0]
    import_count = struct.unpack_from("<I", data, 28)[0]
    import_offset = struct.unpack_from("<I", data, 32)[0]

    # Name table
    pos = name_offset
    for _ in range(name_count):
        L = data[pos]; pos += 1
        s = data[pos:pos + L - 1].decode("latin-1", errors="replace")
        pos += L + 4  # null + flags u32
        pkg.names.append(s)

    # Import table
    pos = import_offset
    for _ in range(import_count):
        cp, pos = _read_compact_index(data, pos)
        cn, pos = _read_compact_index(data, pos)
        oi = struct.unpack_from("<i", data, pos)[0]; pos += 4
        on, pos = _read_compact_index(data, pos)
        pkg.imports.append(Import(cp, cn, oi, on))

    # Export table
    pos = export_offset
    for _ in range(export_count):
        ci, pos = _read_compact_index(data, pos)
        si, pos = _read_compact_index(data, pos)
        pi = struct.unpack_from("<I", data, pos)[0]; pos += 4
        on, pos = _read_compact_index(data, pos)
        of = struct.unpack_from("<I", data, pos)[0]; pos += 4
        sz, pos = _read_compact_index(data, pos)
        so = 0
        if sz > 0:
            so, pos = _read_compact_index(data, pos)
        pkg.exports.append(Export(ci, si, pi, on, of, sz, so))

    return pkg


def class_name_for_export(pkg: Package, exp: Export) -> str:
    """Return the human-readable class name of an export."""
    ci = exp.class_index
    if ci == 0:
        return "<Class>"  # native class, the export IS a class
    if ci < 0:
        # import index: ci=-1 means imports[0]
        idx = -ci - 1
        if 0 <= idx < len(pkg.imports):
            imp = pkg.imports[idx]
            return pkg.names[imp.object_name]
    else:
        idx = ci - 1
        if 0 <= idx < len(pkg.exports):
            return pkg.names[pkg.exports[idx].object_name]
    return "?"


def name_of_export(pkg: Package, exp: Export) -> str:
    return pkg.names[exp.object_name]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--filter-class", help="only show exports of this class (e.g., Texture, Font)")
    ap.add_argument("--top", type=int, default=20, help="show top-N largest exports")
    args = ap.parse_args()
    for path in args.paths:
        pkg = open_pkg(path)
        print(f"\n=== {path} ===")
        print(f"  ver={pkg.package_ver:#x}  names={len(pkg.names)}  "
              f"imports={len(pkg.imports)}  exports={len(pkg.exports)}")
        # group exports by class
        from collections import Counter
        class_counts = Counter(class_name_for_export(pkg, e) for e in pkg.exports)
        print(f"  exports by class (top 12):")
        for c, n in class_counts.most_common(12):
            print(f"    {c}: {n}")
        # filter / sort
        rows = [(class_name_for_export(pkg, e), name_of_export(pkg, e), e.serial_size, e.serial_offset, idx)
                for idx, e in enumerate(pkg.exports)]
        if args.filter_class:
            rows = [r for r in rows if r[0] == args.filter_class]
        rows.sort(key=lambda r: -r[2])
        print(f"  top {args.top} biggest{(' '+args.filter_class) if args.filter_class else ''} exports:")
        for cls, name, size, offset, idx in rows[:args.top]:
            print(f"    [{idx:>5}] {cls:<24} {name:<32} size={size:>9,} offset={offset:#011x}")
