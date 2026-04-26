"""Dump the *FileIndex.idx manifest at entry 0 of each AFS, plus a quick structural sniff."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from afs import Afs


def main() -> int:
    out_dir = Path("work/scratch/idx")
    out_dir.mkdir(parents=True, exist_ok=True)
    for arg in sys.argv[1:]:
        afs = Afs.open(arg)
        names = afs.read_filename_toc() or []
        with open(arg, "rb") as fh:
            data = afs.read_entry(0, fh)
        idx_name = names[0] if names else "entry0.bin"
        tag = Path(arg).stem  # e.g. MUSIC
        region = "usa" if "usa/" in arg else ("kr" if "kr/" in arg else "x")
        out = out_dir / f"{region}_{tag}__{idx_name}"
        out.write_bytes(data)
        print(f"=== {arg} entry0={idx_name} size={len(data)} -> {out}")
        # try to read magic + a count field
        magic = data[:8]
        print(f"    magic: {magic!r}  hex: {data[:16].hex()}")
        # most CRI/Softmax index files start with an 8-byte ASCII tag
        # then a u32 count; print first 6 candidate u32 values
        import struct as _s

        if len(data) >= 32:
            words = _s.unpack_from("<8I", data, 8)
            print(f"    u32 after tag: {words}")
        # if the body looks like fixed-stride records, try to detect stride
        # by looking for a recurring null byte pattern at regular intervals
        # (cheap heuristic only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
