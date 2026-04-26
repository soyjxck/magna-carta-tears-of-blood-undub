"""SofDec demux / mux wrappers for the cutscene undub pipeline.

Demux uses ffmpeg (-c copy) to extract elementary streams:
    in.SFD  ->  out.m1v  (raw MPEG-1 video)
                out.sfa  (CRI ADX with `(c)CRI` watermark at offset 0x11A)

Mux uses an external C binary (nebulas-star/SFD_Muxer, MIT-style C source we
patched + build under work/tools/SFD_Muxer/SFD_Muxer). One bug fix applied:
the original C source declares DTS_forecast / picture_num_basic / etc. on the
stack without zero-init — undefined on macOS/clang. Fixed in our local copy.

A pure-Python port of the muxer is on the TODO; this shim is the bridge.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUXER_BIN = ROOT / "work" / "tools" / "SFD_Muxer" / "SFD_Muxer"
MUXER_SRC = ROOT / "work" / "tools" / "SFD_Muxer" / "Code" / "SFD_Muxer.c"


def ensure_muxer() -> Path:
    """Build the C muxer if it isn't on disk. Returns the binary path.

    On first run we need: clone of nebulas-star/SFD_Muxer at
    work/tools/SFD_Muxer/, the io.h→unistd.h + _access→access patches applied,
    and the DTS-array zero-init patch applied. `patch.py setup` does that.
    """
    if MUXER_BIN.exists():
        return MUXER_BIN
    if not MUXER_SRC.exists():
        raise FileNotFoundError(
            f"SFD_Muxer source not found at {MUXER_SRC}. "
            "Clone nebulas-star/SFD_Muxer into work/tools/ first."
        )
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if cc is None:
        raise RuntimeError("no C compiler on PATH (cc/clang/gcc)")
    subprocess.run(
        [
            cc,
            "-O2",
            "-Wno-tautological-constant-out-of-range-compare",
            "-Wno-implicit-function-declaration",
            "-o",
            str(MUXER_BIN),
            str(MUXER_SRC),
        ],
        check=True,
    )
    return MUXER_BIN


def demux(sfd: Path, out_m1v: Path, out_sfa: Path, ffmpeg: str = "ffmpeg") -> None:
    """Split a SofDec SFD into raw MPEG-1 video + CRI ADX audio."""
    out_m1v.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(sfd),
            "-map", "0:v", "-c:v", "copy", str(out_m1v),
            "-map", "0:a", "-c:a", "copy", "-f", "adx", str(out_sfa),
        ],
        check=True,
    )


def mux(m1v: Path, sfa: Path, out_sfd: Path) -> None:
    """Combine MPEG-1 video + CRI ADX (SFA-format) into a SofDec PS."""
    bin_path = ensure_muxer()
    out_sfd.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [str(bin_path), "-y", "-v", str(m1v), "-a", str(sfa), "-o", str(out_sfd)],
        check=True,
    )


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demux")
    d.add_argument("sfd", type=Path)
    d.add_argument("--out-m1v", type=Path, required=True)
    d.add_argument("--out-sfa", type=Path, required=True)
    m = sub.add_parser("mux")
    m.add_argument("--m1v", type=Path, required=True)
    m.add_argument("--sfa", type=Path, required=True)
    m.add_argument("--out", type=Path, required=True)
    rt = sub.add_parser("roundtrip", help="demux + mux + size compare against original")
    rt.add_argument("sfd", type=Path)
    rt.add_argument("--workdir", type=Path, default=Path("work/scratch/roundtrip"))
    args = ap.parse_args()

    if args.cmd == "demux":
        demux(args.sfd, args.out_m1v, args.out_sfa)
    elif args.cmd == "mux":
        mux(args.m1v, args.sfa, args.out)
    elif args.cmd == "roundtrip":
        wd = args.workdir
        wd.mkdir(parents=True, exist_ok=True)
        m1v = wd / (args.sfd.stem + ".m1v")
        sfa = wd / (args.sfd.stem + ".sfa")
        out = wd / (args.sfd.stem + "_remux.SFD")
        demux(args.sfd, m1v, sfa)
        mux(m1v, sfa, out)
        orig_size = args.sfd.stat().st_size
        new_size = out.stat().st_size
        print(f"orig:  {orig_size:>12,} bytes")
        print(f"remux: {new_size:>12,} bytes  (Δ={new_size - orig_size:+,})")
