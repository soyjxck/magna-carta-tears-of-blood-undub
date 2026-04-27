"""Surgically replace specific exports in USA MrtsEngine.u with KR's
counterparts. Useful for swapping font textures/objects without disturbing
the rest of the engine bytecode that the USA boot ELF was compiled against.

Two modes:
  - 'in-place':   only export pairs of identical size (e.g., Texture0).
  - 'rewrite':    replace any export, regardless of size — requires
                  appending the new bytes past the file's original end and
                  updating the export-table entry's (serial_size,
                  serial_offset) to point at the appended data.
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from ue2_pkg import open_pkg, name_of_export, class_name_for_export


def find_export(pkg, want_class: str, want_name: str) -> int | None:
    for i, e in enumerate(pkg.exports):
        if class_name_for_export(pkg, e) == want_class and name_of_export(pkg, e) == want_name:
            return i
    return None


def inject_in_place(usa_path: Path, kr_path: Path, out_path: Path,
                    swap_pairs: list[tuple[str, str]]) -> dict:
    """Byte-swap exports between USA and KR where sizes match exactly.
    swap_pairs is a list of (class, name) tuples to attempt swapping.
    """
    usa = open_pkg(usa_path)
    kr = open_pkg(kr_path)
    out = bytearray(usa.data)
    summary = []
    for cls, name in swap_pairs:
        ui = find_export(usa, cls, name)
        ki = find_export(kr, cls, name)
        if ui is None or ki is None:
            summary.append((name, "not found"))
            continue
        ue = usa.exports[ui]
        ke = kr.exports[ki]
        if ue.serial_size != ke.serial_size:
            summary.append((name, f"size mismatch USA={ue.serial_size} KR={ke.serial_size}"))
            continue
        kr_bytes = kr.data[ke.serial_offset:ke.serial_offset + ke.serial_size]
        out[ue.serial_offset:ue.serial_offset + ue.serial_size] = kr_bytes
        summary.append((name, f"swapped {len(kr_bytes)} bytes at USA offset 0x{ue.serial_offset:x}"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(bytes(out))
    return {"out_size": len(out), "swaps": summary}


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--usa", type=Path, required=True)
    ap.add_argument("--kr",  type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--swap", action="append", default=[],
                    help="format: Class:Name (e.g., Texture:Texture0)")
    args = ap.parse_args()
    pairs = [tuple(s.split(":", 1)) for s in args.swap]
    res = inject_in_place(args.usa, args.kr, args.out, pairs)
    print(f"wrote {args.out} ({res['out_size']:,} B)")
    for name, status in res["swaps"]:
        print(f"  {name}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
