"""Magna Carta: Tears of Blood — Korean undub patch builder (FMA-style pipeline).

Subcommands
-----------
  setup        Extract both ISOs (work/usa, work/kr) and build ffmpeg with libass.

  transcribe   Run Whisper on every USA cutscene's English audio. Writes
               subs/<basename>.srt + subs/<basename>.ass. Skip any SFD that
               already has both files (use --force to redo).

  cutscenes    Build undubbed SFDs (KR video + KR audio + English ASS burned
               in) for every cutscene. Outputs land under build/cutscenes/.

  build-iso    Patch the original USA ISO with the new cutscenes:
                 - small enough to fit the original slot -> in-place write
                 - too big -> append past original ISO end, patch ISO9660
                   directory entry to point at the new LBA + new size
               Output: build/magna-carta-tears-of-blood-undub.iso

  xdelta       xdelta3 -e -9 -S djw  USA ISO -> patched ISO.

  full         transcribe (skip-existing) + cutscenes + build-iso + xdelta.

Inputs come from `roms/Magna Carta - Tears of Blood (USA).iso` and
`roms/Magna Carta - Jinhongui Seongheun (Korea).iso` by default.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "lib"))

from cutscene_pipeline import run_all as run_cutscenes
from ffmpeg_libass import find_or_build_ffmpeg
from iso_patcher import patch_iso

DEFAULT_USA_ISO = ROOT / "roms" / "Magna Carta - Tears of Blood (USA).iso"
DEFAULT_KR_ISO = ROOT / "roms" / "Magna Carta - Jinhongui Seongheun (Korea).iso"
WORK = ROOT / "work"
BUILD = ROOT / "build"
SUBS = ROOT / "subs"
PATCHED_ISO = BUILD / "magna-carta-tears-of-blood-undub.iso"
PATCH_XDELTA = BUILD / "magna-carta-tears-of-blood-undub.xdelta"


def _ensure_extracted(iso: Path, dest: Path) -> None:
    if dest.exists() and any(dest.iterdir()):
        return
    dest.mkdir(parents=True, exist_ok=True)
    print(f"  extracting {iso.name} -> {dest}")
    subprocess.run(["7z", "x", "-y", f"-o{dest}", str(iso)], check=True)


def cmd_setup(args: argparse.Namespace) -> int:
    _ensure_extracted(args.usa_iso, WORK / "usa")
    _ensure_extracted(args.kr_iso, WORK / "kr")
    print(f"  ensuring ffmpeg with libass ...")
    bin_path = find_or_build_ffmpeg()
    print(f"  ffmpeg ready: {bin_path}")
    return 0


def cmd_transcribe(args: argparse.Namespace) -> int:
    cmd = [sys.executable, str(ROOT / "lib" / "whisper_transcribe.py"), "--all", "--model", args.model]
    if args.force:
        cmd.append("--force")
    subprocess.run(cmd, check=True)
    return 0


def cmd_cutscenes(args: argparse.Namespace) -> int:
    out = run_cutscenes(work_dir=BUILD / "cutscenes")
    print(f"\nbuilt {len(out)} cutscenes")
    return 0


def cmd_build_iso(args: argparse.Namespace) -> int:
    cutscene_dir = BUILD / "cutscenes"
    if not cutscene_dir.exists():
        sys.exit("run `patch.py cutscenes` first")
    replacements: dict[str, Path] = {}
    for sub in cutscene_dir.iterdir():
        if not sub.is_dir():
            continue
        candidate = sub / f"{sub.name}_undub.SFD"
        if not candidate.exists():
            continue
        # Find which subdir (MOVIE18 / MOVIE99) the original lives in.
        for movie_dir in ("MOVIE18", "MOVIE99"):
            if (WORK / "usa" / movie_dir / f"{sub.name}.SFD").exists():
                replacements[f"/{movie_dir}/{sub.name}.SFD"] = candidate
                break
    print(f"  applying {len(replacements)} cutscene patches to ISO ...")
    in_place, relocated = patch_iso(args.usa_iso, PATCHED_ISO, replacements)
    print(f"\nbuilt {PATCHED_ISO} ({PATCHED_ISO.stat().st_size:,} B)")
    print(f"  in-place: {in_place}, relocated: {relocated}")
    return 0


def cmd_xdelta(args: argparse.Namespace) -> int:
    if not PATCHED_ISO.exists():
        sys.exit("run `patch.py build-iso` first")
    subprocess.run(
        ["xdelta3", "-e", "-9", "-S", "djw", "-f",
         "-s", str(args.usa_iso), str(PATCHED_ISO), str(PATCH_XDELTA)],
        check=True,
    )
    print(f"\nbuilt {PATCH_XDELTA} ({PATCH_XDELTA.stat().st_size:,} B)")
    print(f"apply with:  xdelta3 -d -s '<USA ISO>' {PATCH_XDELTA.name} <output.iso>")
    return 0


def cmd_full(args: argparse.Namespace) -> int:
    cmd_setup(args)
    cmd_transcribe(args)
    cmd_cutscenes(args)
    cmd_build_iso(args)
    cmd_xdelta(args)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="patch.py", description=__doc__.splitlines()[0])
    ap.add_argument("--usa-iso", type=Path, default=DEFAULT_USA_ISO)
    ap.add_argument("--kr-iso", type=Path, default=DEFAULT_KR_ISO)
    ap.add_argument("--model", default="small", help="Whisper model: tiny/base/small/medium/large")
    ap.add_argument("--force", action="store_true", help="re-run steps that would otherwise skip")

    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="extract ISOs + build ffmpeg-libass").set_defaults(func=cmd_setup)
    sub.add_parser("transcribe", help="Whisper on every USA cutscene -> subs/").set_defaults(func=cmd_transcribe)
    sub.add_parser("cutscenes", help="build all undubbed SFDs").set_defaults(func=cmd_cutscenes)
    sub.add_parser("build-iso", help="patch USA ISO with cutscenes").set_defaults(func=cmd_build_iso)
    sub.add_parser("xdelta", help="USA ISO -> patched ISO -> xdelta3").set_defaults(func=cmd_xdelta)
    sub.add_parser("full", help="setup + transcribe + cutscenes + build-iso + xdelta").set_defaults(func=cmd_full)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
