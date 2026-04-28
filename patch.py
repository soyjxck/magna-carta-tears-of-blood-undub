"""Magna Carta: Tears of Blood — Korean / Japanese undub patch builder.

Subcommands
-----------
  setup        Extract USA + source-region ISOs and build ffmpeg with libass.

  cutscenes    Build undubbed SFDs (source-region video + audio + English
               ASS burned in) for every cutscene. Reads pre-generated subs
               from `subs/<source>/`. Outputs land under build/cutscenes-<source>/.

  build-iso    Build the full undub ISO:
                 Phase 1 (cutscenes): patch in re-encoded SFDs.
                 Phase 3 (in-game): swap source LINEAR.AFS + MUSIC.AFS for
                 the chosen voice/level data, and a source-base hybrid
                 SHIP.AFS with USA text overlays for English UI/dialog/items.
                 USA boot ELF + USA FILE.AFS stay (USA engine + fonts).
               Output: build/magna-carta-tears-of-blood-undub-<source>.iso

  xdelta       xdelta3 -e -9 -S djw  USA ISO -> patched ISO ->
               build/magna-carta-tears-of-blood-undub-<source>.xdelta

  full         setup + cutscenes + build-iso + xdelta.

Source region (--source kr | jp; default kr) selects which region's voice +
scene data is used for the undub. Inputs:
  roms/Magna Carta - Tears of Blood (USA).iso
  roms/Magna Carta - Jinhongui Seongheun (Korea).iso        (--source kr)
  roms/Magna Carta (Japan).iso                              (--source jp)
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "lib"))

from cutscenes import run_all as run_cutscenes
from ffmpeg import find_or_build_ffmpeg
from iso import patch_iso
from ship import build as build_ship

DEFAULT_USA_ISO = ROOT / "roms" / "Magna Carta - Tears of Blood (USA).iso"
DEFAULT_SOURCE_ISOS = {
    "kr": ROOT / "roms" / "Magna Carta - Jinhongui Seongheun (Korea).iso",
    "jp": ROOT / "roms" / "Magna Carta (Japan).iso",
}
SUBS_DIRS = {"kr": ROOT / "subs" / "korean", "jp": ROOT / "subs" / "japanese"}

WORK = ROOT / "work"
BUILD = ROOT / "build"


def _paths(source: str) -> dict[str, Path]:
    """Resolve every per-region path from a single `source` flag."""
    src_root = WORK / source
    return {
        "src_root": src_root,
        "src_iso": DEFAULT_SOURCE_ISOS[source],
        "src_linear": src_root / "LINEAR.AFS",
        "src_music": src_root / "MUSIC.AFS",
        "src_ship": src_root / "SHIP.AFS",
        "subs_dir": SUBS_DIRS[source],
        "cutscenes_dir": BUILD / f"cutscenes-{source}",
        "ship_hybrid": BUILD / f"{source}_base" / "SHIP.AFS",
        "patched_iso": BUILD / f"magna-carta-tears-of-blood-undub-{source}.iso",
        "patch_xdelta": BUILD / f"magna-carta-tears-of-blood-undub-{source}.xdelta",
    }


def _ensure_extracted(iso: Path, dest: Path) -> None:
    if dest.exists() and any(dest.iterdir()):
        return
    dest.mkdir(parents=True, exist_ok=True)
    print(f"  extracting {iso.name} -> {dest}")
    subprocess.run(["7z", "x", "-y", f"-o{dest}", str(iso)], check=True)


def cmd_setup(args: argparse.Namespace) -> int:
    p = _paths(args.source)
    _ensure_extracted(args.usa_iso, WORK / "usa")
    _ensure_extracted(p["src_iso"], p["src_root"])
    print(f"  ensuring ffmpeg with libass ...")
    bin_path = find_or_build_ffmpeg()
    print(f"  ffmpeg ready: {bin_path}")
    return 0


def cmd_cutscenes(args: argparse.Namespace) -> int:
    p = _paths(args.source)
    out = run_cutscenes(work_dir=p["cutscenes_dir"], src_root=p["src_root"], subs_dir=p["subs_dir"])
    print(f"\nbuilt {len(out)} cutscenes for source={args.source}")
    return 0


def cmd_build_iso(args: argparse.Namespace) -> int:
    p = _paths(args.source)
    replacements: dict[str, Path] = {}

    # Phase 1: cutscenes (re-encoded source video + audio with EN subs burned in)
    cutscene_dir = p["cutscenes_dir"]
    if cutscene_dir.exists():
        for sub in cutscene_dir.iterdir():
            if not sub.is_dir():
                continue
            candidate = sub / f"{sub.name}_undub.SFD"
            if not candidate.exists():
                continue
            for movie_dir in ("MOVIE18", "MOVIE99"):
                if (WORK / "usa" / movie_dir / f"{sub.name}.SFD").exists():
                    replacements[f"/{movie_dir}/{sub.name}.SFD"] = candidate
                    break
        n_movies = len([k for k in replacements if k.startswith('/MOVIE')])
        print(f"  Phase 1: {n_movies} cutscene SFD swaps queued")
    else:
        print(f"  Phase 1: no cutscenes built — run `patch.py cutscenes --source {args.source}` first")

    # Phase 3: build hybrid SHIP.AFS, then queue the AFS swaps
    print(f"  Phase 3: building {args.source}-base hybrid SHIP.AFS with USA text overlays")
    build_ship(out_path=p["ship_hybrid"], src_ship=p["src_ship"])
    if not p["ship_hybrid"].exists():
        sys.exit(f"hybrid SHIP.AFS missing at {p['ship_hybrid']}")
    if not p["src_linear"].exists() or not p["src_music"].exists():
        sys.exit(f"source LINEAR.AFS / MUSIC.AFS missing — run `patch.py setup --source {args.source}` first")
    replacements["/LINEAR.AFS"] = p["src_linear"]
    replacements["/MUSIC.AFS"] = p["src_music"]
    replacements["/SHIP.AFS"] = p["ship_hybrid"]

    print(f"\n  applying {len(replacements)} swaps to ISO ...")
    in_place, relocated = patch_iso(args.usa_iso, p["patched_iso"], replacements)
    print(f"\nbuilt {p['patched_iso']} ({p['patched_iso'].stat().st_size:,} B)")
    print(f"  in-place: {in_place}, relocated: {relocated}")
    return 0


def cmd_xdelta(args: argparse.Namespace) -> int:
    p = _paths(args.source)
    if not p["patched_iso"].exists():
        sys.exit(f"run `patch.py build-iso --source {args.source}` first")
    subprocess.run(
        ["xdelta3", "-e", "-9", "-S", "djw", "-f",
         "-s", str(args.usa_iso), str(p["patched_iso"]), str(p["patch_xdelta"])],
        check=True,
    )
    print(f"\nbuilt {p['patch_xdelta']} ({p['patch_xdelta'].stat().st_size:,} B)")
    print(f"apply with:  xdelta3 -d -s '<USA ISO>' {p['patch_xdelta'].name} <output.iso>")
    return 0


def cmd_full(args: argparse.Namespace) -> int:
    cmd_setup(args)
    cmd_cutscenes(args)
    cmd_build_iso(args)
    cmd_xdelta(args)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="patch.py", description=__doc__.splitlines()[0])
    ap.add_argument("--source", choices=["kr", "jp"], default="kr",
                    help="source region for voice/scene data (default: kr)")
    ap.add_argument("--usa-iso", type=Path, default=DEFAULT_USA_ISO)

    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="extract ISOs + build ffmpeg-libass").set_defaults(func=cmd_setup)
    sub.add_parser("cutscenes", help="build all undubbed SFDs from subs/<source>/").set_defaults(func=cmd_cutscenes)
    sub.add_parser("build-iso", help="patch USA ISO with cutscenes + AFS swaps").set_defaults(func=cmd_build_iso)
    sub.add_parser("xdelta", help="USA ISO -> patched ISO -> xdelta3").set_defaults(func=cmd_xdelta)
    sub.add_parser("full", help="setup + cutscenes + build-iso + xdelta").set_defaults(func=cmd_full)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
