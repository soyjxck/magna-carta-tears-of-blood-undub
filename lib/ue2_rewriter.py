"""Rewrite a UE2 package: take an open Package + a dict of {(class, name): new_bytes}
and produce a new package binary where those exports' data has been replaced
(possibly with different sizes) and the export table updated to match.

Strategy
--------
Since USA boot ELF references exports by NAME (via the name table → export
table lookup), the on-disk LAYOUT of export data doesn't matter — only the
export table's serial_offset/size for each export must be correct.

We rebuild the package as:
    [40-byte header]
    [original name table verbatim]      @ 0x40
    [original import table verbatim]    @ original import_offset
    [new export table]                  @ original export_offset
    [export data section, contiguous]   @ end of new export table

For each export, write its bytes into the new export-data section in entry
order. The serial_offset for export[i] = (export_data_start) + sum(sizes[0..i-1]).
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from ue2_pkg import Package, open_pkg, name_of_export, class_name_for_export, _read_compact_index


def _write_compact_index(value: int) -> bytes:
    """Inverse of _read_compact_index. Encodes a signed integer."""
    sign = 0x80 if value < 0 else 0x00
    v = abs(value)
    # First byte: 6 bits of value + sign + continuation flag
    b0 = sign | (v & 0x3F)
    v >>= 6
    if v == 0:
        return bytes([b0])
    b0 |= 0x40  # continuation
    out = [b0]
    while True:
        byte = v & 0x7F
        v >>= 7
        if v:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def rewrite_package(pkg: Package, replacements: dict[tuple[str, str], bytes],
                    out_path: Path) -> dict:
    """Replace export data for given (class, name) keys, rewrite the package
    with a compact layout: [header][name table][import table][export table]
    [export data section, contiguous, in entry order]."""
    data = pkg.data

    # --- 1. Resolve replacement targets to export indices ---
    replace_idx: dict[int, bytes] = {}
    for i, e in enumerate(pkg.exports):
        cls = class_name_for_export(pkg, e)
        nm = name_of_export(pkg, e)
        if (cls, nm) in replacements:
            replace_idx[i] = replacements[(cls, nm)]

    # --- 2. Read original header / table boundaries ---
    name_count = struct.unpack_from("<I", data, 12)[0]
    orig_name_offset = struct.unpack_from("<I", data, 16)[0]
    export_count = struct.unpack_from("<I", data, 20)[0]
    orig_export_offset = struct.unpack_from("<I", data, 24)[0]
    import_count = struct.unpack_from("<I", data, 28)[0]
    orig_import_offset = struct.unpack_from("<I", data, 32)[0]

    # Compute the EXACT size of the name table by walking entries.
    # Each entry: u8 length, ASCII string + 0x00, u32 flags.
    pos = orig_name_offset
    for _ in range(name_count):
        L = data[pos]; pos += 1
        pos += L + 4
    actual_name_table_size = pos - orig_name_offset
    name_blob = data[orig_name_offset:orig_name_offset + actual_name_table_size]
    # The import table sits before the export table — same span as original.
    orig_import_table_size = orig_export_offset - orig_import_offset
    import_blob = data[orig_import_offset:orig_import_offset + orig_import_table_size]

    # --- 3. Per-export new sizes + per-export new data blob ---
    new_sizes = []
    new_data_blobs = []
    for i, e in enumerate(pkg.exports):
        if i in replace_idx:
            blob = replace_idx[i]
        else:
            blob = data[e.serial_offset:e.serial_offset + e.serial_size]
        new_sizes.append(len(blob))
        new_data_blobs.append(blob)

    # --- 4. Layout planning — pack tables contiguously after the header ---
    # We keep the original name_offset (typically 0x40 = right after header).
    # New import_offset = right after name table. New export_offset = right
    # after import table. New data section = right after export table.
    new_name_offset = orig_name_offset
    new_import_offset = new_name_offset + actual_name_table_size
    new_export_offset = new_import_offset + orig_import_table_size

    def encode_export_table(offsets: list[int]) -> bytes:
        out = bytearray()
        for i, e in enumerate(pkg.exports):
            out += _write_compact_index(e.class_index)
            out += _write_compact_index(e.super_index)
            out += struct.pack("<I", e.package_index)
            out += _write_compact_index(e.object_name)
            out += struct.pack("<I", e.object_flags)
            sz = new_sizes[i]
            out += _write_compact_index(sz)
            if sz > 0:
                out += _write_compact_index(offsets[i])
        return bytes(out)

    # Iterate to stabilize export-table size (compact-int offset lengths can
    # change when offsets cross 1-byte / 2-byte / 3-byte boundaries).
    table_size = len(encode_export_table([0] * export_count))
    for _ in range(8):
        data_section_start = new_export_offset + table_size
        cursor = data_section_start
        new_offsets = []
        for sz in new_sizes:
            new_offsets.append(cursor if sz > 0 else 0)
            cursor += sz
        export_table = encode_export_table(new_offsets)
        if len(export_table) == table_size:
            break
        table_size = len(export_table)
    else:
        raise RuntimeError("export table encoding didn't converge")

    # --- 5. Assemble: header (with patched offsets) + tables + data ---
    out = bytearray(data[:orig_name_offset])  # header (preserves bytes past 0x28)
    # patch import_offset (at 0x20) and export_offset (at 0x18) in header
    struct.pack_into("<I", out, 24, new_export_offset)
    struct.pack_into("<I", out, 32, new_import_offset)

    out += name_blob
    assert len(out) == new_import_offset, f"after names: {len(out):#x} vs {new_import_offset:#x}"
    out += import_blob
    assert len(out) == new_export_offset, f"after imports: {len(out):#x} vs {new_export_offset:#x}"
    out += export_table
    assert len(out) == data_section_start, f"after exports: {len(out):#x} vs {data_section_start:#x}"
    for blob in new_data_blobs:
        out += blob

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(out))
    return {
        "out_size": len(out),
        "replacements": len(replace_idx),
        "new_name_offset": new_name_offset,
        "new_import_offset": new_import_offset,
        "new_export_offset": new_export_offset,
        "export_table_size": len(export_table),
        "data_section_start": data_section_start,
    }


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, required=True, help="package to rewrite (USA MrtsEngine.u)")
    ap.add_argument("--from", dest="src_other", type=Path, required=True,
                    help="package to take replacements from (KR MrtsEngine.u)")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--swap", action="append", default=[],
                    help="format: Class:Name (e.g., Font:NormalFont)")
    args = ap.parse_args()

    src = open_pkg(args.src)
    other = open_pkg(args.src_other)
    repl: dict[tuple[str, str], bytes] = {}
    for s in args.swap:
        cls, nm = s.split(":", 1)
        # find in 'other'
        for e in other.exports:
            if class_name_for_export(other, e) == cls and name_of_export(other, e) == nm:
                repl[(cls, nm)] = other.data[e.serial_offset:e.serial_offset + e.serial_size]
                break
        else:
            print(f"  WARN: {cls}:{nm} not found in {args.src_other}")
    res = rewrite_package(src, repl, args.out)
    print(f"wrote {args.out} ({res['out_size']:,} B)  swaps={res['replacements']}  "
          f"export_table={res['export_table_size']:,} B  data_section_start=0x{res['data_section_start']:x}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
