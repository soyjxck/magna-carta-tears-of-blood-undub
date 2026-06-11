"""Magna Carta: Tears of Blood — Korean / Japanese undub patch builder.

Subcommands
-----------
  setup              Extract USA + source-region ISOs and build ffmpeg with libass.

  cutscenes          Build undubbed SFDs (source-region video + audio + English
                     ASS burned in) for every cutscene. Reads pre-generated subs
                     from `subs/<source>/`. Outputs land under
                     build/cutscenes-<source>/.

  build-iso          Build the full undub ISO:
                       Phase 1 (cutscenes): patch in re-encoded SFDs.
                       Phase 3 (in-game): swap source LINEAR.AFS + MUSIC.AFS for
                       the chosen voice/level data, plus source-base hybrid
                       SHIP.AFS + LINEAR.AFS with USA overlays for English
                       UI/dialog/items + menu textures.
                       USA boot ELF + USA FILE.AFS stay (USA engine + fonts).
                     Output: build/magna-carta-tears-of-blood-undub-<source>.iso

                     With --translations: applies edits from translations/<ext>/
                     catalogs (see translate-extract). Default builds use raw
                     USA bytes — vanilla undub.

  xdelta             xdelta3 -e -9 -S djw  USA ISO -> patched ISO ->
                     build/magna-carta-tears-of-blood-undub-<source>.xdelta

  full               setup + cutscenes + build-iso + xdelta.

  translate-extract  Dump per-file translation catalogs to translations/<ext>/.
                     Each catalog stores USA English + KR/JP reference text
                     for one SHIP.AFS file family (.fpb dialog, .cht phone
                     conversations, .tui UI labels, etc.). Edit the `en`
                     fields, then re-run `build-iso --translations`.

  translate-validate Pre-build sanity check for translations/ — reports cap
                     violations, latin-1 errors, dropped $n tokens, and
                     accidentally-edited read-only fields with file:record
                     precision. Exits non-zero on errors.

  translate-status   Per-extension progress table: how many records are
                     edited vs untouched. See TRANSLATING.md.

  dump-mkv           Dump KR or JP cutscenes to MKV files for review,
                     optionally with English subs hardsubbed via libass.
                     Output: build/cutscene-dumps/<region>[-hardsub]/.

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

from cutscenes import SUB_DIR_FOR, dump_all_to_mkv
from cutscenes import run_all as run_cutscenes
from ffmpeg import find_or_build_ffmpeg
from iso import patch_iso
from linear import build as build_linear
from ship import build as build_ship
from translate import (
    audit_all,
    build_file_afs_with_celfid,
    extract_all,
    extract_celfid_catalog,
    format_status_table,
)

DEFAULT_USA_ISO = ROOT / "roms" / "Magna Carta - Tears of Blood (USA).iso"
DEFAULT_SOURCE_ISOS = {
    "kr": ROOT / "roms" / "Magna Carta - Jinhongui Seongheun (Korea).iso",
    "jp": ROOT / "roms" / "Magna Carta (Japan).iso",
}

WORK = ROOT / "work"
BUILD = ROOT / "build"


def _paths(source: str, hardsub: bool = True) -> dict[str, Path]:
    """Resolve every per-region path from a single `source` flag.

    When ``hardsub=False`` the cutscene cache and patched ISO/xdelta
    outputs use a ``-raw`` suffix so the two variants can coexist on
    disk without colliding.
    """
    src_root = WORK / source
    suffix = "" if hardsub else "-raw"
    return {
        "src_root": src_root,
        "src_iso": DEFAULT_SOURCE_ISOS[source],
        "src_linear": src_root / "LINEAR.AFS",
        "src_music": src_root / "MUSIC.AFS",
        "src_ship": src_root / "SHIP.AFS",
        "subs_dir": SUB_DIR_FOR[source],
        "cutscenes_dir": BUILD / f"cutscenes-{source}{suffix}",
        "linear_hybrid": BUILD / f"{source}_base" / "LINEAR.AFS",
        "ship_hybrid": BUILD / f"{source}_base" / "SHIP.AFS",
        "patched_iso": BUILD / f"magna-carta-tears-of-blood-undub-{source}{suffix}.iso",
        "patch_xdelta": BUILD / f"magna-carta-tears-of-blood-undub-{source}{suffix}.xdelta",
    }


def _ensure_extracted(iso: Path, dest: Path) -> None:
    if dest.exists() and any(dest.iterdir()):
        return
    dest.mkdir(parents=True, exist_ok=True)
    print(f"  extracting {iso.name} -> {dest}")
    subprocess.run(["7z", "x", "-y", f"-o{dest}", str(iso)], check=True)


def _resolve_dir(arg: str) -> Path:
    """Resolve a CLI directory argument relative to ROOT unless it's absolute."""
    p = Path(arg)
    return p if p.is_absolute() else ROOT / p


def _require_usa_ship() -> Path:
    """Return the extracted USA SHIP.AFS, exiting if `setup` hasn't run yet."""
    usa = WORK / "usa" / "SHIP.AFS"
    if not usa.exists():
        sys.exit("USA SHIP.AFS missing — run `patch.py setup` first")
    return usa


def _use_hardsub(args: argparse.Namespace) -> bool:
    """True unless --no-hardsub was passed (default: burn EN subs into video)."""
    return not getattr(args, "no_hardsub", False)


def cmd_setup(args: argparse.Namespace) -> int:
    p = _paths(args.source)
    _ensure_extracted(args.usa_iso, WORK / "usa")
    _ensure_extracted(p["src_iso"], p["src_root"])
    print("  ensuring ffmpeg with libass ...")
    bin_path = find_or_build_ffmpeg()
    print(f"  ffmpeg ready: {bin_path}")
    return 0


def cmd_cutscenes(args: argparse.Namespace) -> int:
    hardsub = _use_hardsub(args)
    p = _paths(args.source, hardsub=hardsub)
    out = run_cutscenes(work_dir=p["cutscenes_dir"], src_root=p["src_root"],
                        subs_dir=p["subs_dir"], hardsub=hardsub)
    tag = "" if hardsub else " (raw, no hardsub)"
    print(f"\nbuilt {len(out)} cutscenes for source={args.source}{tag}")
    return 0


def cmd_build_iso(args: argparse.Namespace) -> int:
    hardsub = _use_hardsub(args)
    p = _paths(args.source, hardsub=hardsub)
    replacements: dict[str, Path] = {}

    # Phase 1: cutscenes (re-encoded source video + audio with EN subs burned in)
    cutscene_dir = p["cutscenes_dir"]
    if cutscene_dir.exists():
        # sorted(): relocation LBAs in iso.py are assigned in this order, so
        # directory-listing order must not leak into the output ISO.
        for sub in sorted(cutscene_dir.iterdir()):
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
        print(f"  Phase 1: no cutscenes built — "
              f"run `patch.py cutscenes --source {args.source}` first")

    # Phase 3: build hybrid SHIP.AFS + hybrid LINEAR.AFS, then queue the AFS swaps
    tx_arg = getattr(args, "translations", None)
    tx_dir = _resolve_dir(tx_arg) if tx_arg is not None else None
    if tx_dir is not None:
        if not tx_dir.exists():
            sys.exit(f"--translations {tx_dir} doesn't exist — "
                     f"run `patch.py translate-extract --out {tx_dir.name}` first")
        print(f"  Phase 3: building {args.source}-base hybrid SHIP.AFS "
              f"with USA text overlays + translations from {tx_dir}")
    else:
        print(f"  Phase 3: building {args.source}-base hybrid SHIP.AFS with USA text overlays")
    build_ship(out_path=p["ship_hybrid"], src_ship=p["src_ship"],
               translations_dir=tx_dir)
    if not p["ship_hybrid"].exists():
        sys.exit(f"hybrid SHIP.AFS missing at {p['ship_hybrid']}")
    if not p["src_linear"].exists() or not p["src_music"].exists():
        sys.exit(f"source LINEAR.AFS / MUSIC.AFS missing — "
                 f"run `patch.py setup --source {args.source}` first")
    print(f"  Phase 3: building {args.source}-base hybrid LINEAR.AFS "
          f"with USA texture/staticmesh overlays")
    build_linear(out_path=p["linear_hybrid"], src_linear=p["src_linear"], source=args.source)
    if not p["linear_hybrid"].exists():
        sys.exit(f"hybrid LINEAR.AFS missing at {p['linear_hybrid']}")
    replacements["/LINEAR.AFS"] = p["linear_hybrid"]
    replacements["/MUSIC.AFS"] = p["src_music"]
    replacements["/SHIP.AFS"] = p["ship_hybrid"]

    # Phase 3b: if a celfid.lix translation catalog exists, rebuild FILE.AFS
    # with the patched celfid.lix (character names, item names, etc.).
    if tx_dir is not None and (tx_dir / "celfid.json").exists():
        file_hybrid = build_file_afs_with_celfid(
            WORK / "usa" / "FILE.AFS",
            BUILD / f"{args.source}_base" / "FILE.AFS",
            tx_dir / "celfid.json",
        )
        if file_hybrid is not None:
            replacements["/FILE.AFS"] = file_hybrid
            print(f"  Phase 3b: rebuilt FILE.AFS with patched celfid.lix "
                  f"→ {file_hybrid} ({file_hybrid.stat().st_size:,}B)")

    print(f"\n  applying {len(replacements)} swaps to ISO ...")
    in_place, relocated = patch_iso(args.usa_iso, p["patched_iso"], replacements)
    print(f"\nbuilt {p['patched_iso']} ({p['patched_iso'].stat().st_size:,} B)")
    print(f"  in-place: {in_place}, relocated: {relocated}")
    return 0


def cmd_xdelta(args: argparse.Namespace) -> int:
    hardsub = _use_hardsub(args)
    p = _paths(args.source, hardsub=hardsub)
    if not p["patched_iso"].exists():
        sys.exit(f"run `patch.py build-iso --source {args.source}"
                 f"{' --no-hardsub' if not hardsub else ''}` first")
    subprocess.run(
        ["xdelta3", "-e", "-9", "-S", "djw", "-f",
         "-s", str(args.usa_iso), str(p["patched_iso"]), str(p["patch_xdelta"])],
        check=True,
    )
    print(f"\nbuilt {p['patch_xdelta']} ({p['patch_xdelta'].stat().st_size:,} B)")
    print(f"apply with:  xdelta3 -d -s '<USA ISO>' {p['patch_xdelta'].name} <output.iso>")
    return 0


def cmd_dump_mkv(args: argparse.Namespace) -> int:
    """Dump KR / JP cutscenes as MKV files. Optionally burn English
    subs into the video via libass (hardsub)."""
    regions = tuple(r.strip() for r in args.regions.split(",") if r.strip())
    return dump_all_to_mkv(regions=regions, hardsub=args.hardsub, jobs=args.jobs)


def cmd_translate_extract(args: argparse.Namespace) -> int:
    """Extract per-file translation catalogs from USA SHIP + celfid.lix in
    USA FILE.AFS, with KR/JP refs. Writes to ``translations/`` by default;
    pass ``--out`` to use a different directory (e.g. ``translations-deepl``)."""
    out_dir = _resolve_dir(getattr(args, "out", None) or "translations")

    usa = _require_usa_ship()
    kr  = WORK / "kr"  / "SHIP.AFS"
    jp  = WORK / "jp"  / "SHIP.AFS"
    counts = extract_all(usa,
                         kr if kr.exists() else None,
                         jp if jp.exists() else None,
                         out_dir)
    total = sum(counts.values())
    for ext, n in counts.items():
        print(f"  {ext}: {n} files -> {out_dir.name}/{ext.lstrip('.')}/")

    # celfid.lix: independent catalog for character names + item/UI strings
    # baked into the FILE.AFS startup bundle (the engine reads display names
    # from here, not from SHIP.AFS .cha).
    usa_file = WORK / "usa" / "FILE.AFS"
    kr_file  = WORK / "kr"  / "FILE.AFS"
    jp_file  = WORK / "jp"  / "FILE.AFS"
    if usa_file.exists():
        n_celfid = extract_celfid_catalog(
            usa_file,
            kr_file if kr_file.exists() else None,
            jp_file if jp_file.exists() else None,
            out_dir / "celfid.json",
        )
        print(f"  celfid.lix: {n_celfid} slots -> {out_dir.name}/celfid.json")
        total += 1

    print(f"\n  {total} catalog files total in {out_dir}/.")
    tag = f" --translations {out_dir.name}" if out_dir.name != "translations" else " --translations"
    print(f"  edit *.json `en` fields, then `patch.py build-iso{tag}`.")
    return 0


def cmd_translate_validate(args: argparse.Namespace) -> int:
    """Audit every JSON catalog under translations/ (or a custom dir).
    Prints issues with file:record precision and exits non-zero on errors."""
    usa = _require_usa_ship()
    cat_dir = _resolve_dir(getattr(args, "dir", None) or "translations")
    if not cat_dir.exists():
        sys.exit(f"{cat_dir} missing — run `patch.py translate-extract --out {cat_dir.name}` first")
    usa_file = WORK / "usa" / "FILE.AFS"
    issues, _ = audit_all(usa, cat_dir,
                          usa_file_afs=usa_file if usa_file.exists() else None)
    n_err = sum(1 for i in issues if i.level == "error")
    n_warn = sum(1 for i in issues if i.level == "warn")
    if not issues:
        print("  no issues found.")
        return 0
    for i in issues:
        print(i.format(root=ROOT))
    print(f"\n  {n_err} error(s), {n_warn} warning(s).")
    return 1 if n_err else 0


def cmd_translate_status(args: argparse.Namespace) -> int:
    """Per-extension translation progress: how many records have been edited."""
    usa = _require_usa_ship()
    cat_dir = _resolve_dir(getattr(args, "dir", None) or "translations")
    if not cat_dir.exists():
        sys.exit(f"{cat_dir} missing — run `patch.py translate-extract --out {cat_dir.name}` first")
    usa_file = WORK / "usa" / "FILE.AFS"
    _, status = audit_all(usa, cat_dir,
                          usa_file_afs=usa_file if usa_file.exists() else None)
    if not status:
        print("  no catalogs found under translations/")
        return 0
    print(format_status_table(status))
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

    cs = sub.add_parser("cutscenes", help="build all undubbed SFDs from subs/<source>/")
    cs.add_argument("--no-hardsub", action="store_true",
                    help="skip burning EN subs into video; output raw source-region cutscenes")
    cs.set_defaults(func=cmd_cutscenes)

    bi = sub.add_parser("build-iso", help="patch USA ISO with cutscenes + AFS swaps")
    bi.add_argument("--translations", nargs="?", const="translations", default=None,
                    metavar="DIR",
                    help="apply edits from <DIR>/*.json. With no value: 'translations/'. "
                         "Pass a path to use an alternate catalog (e.g. 'translations-deepl').")
    bi.add_argument("--no-hardsub", action="store_true",
                    help="use raw (no-hardsub) cutscenes; ISO output goes to ...-raw.iso")
    bi.set_defaults(func=cmd_build_iso)

    xd = sub.add_parser("xdelta", help="USA ISO -> patched ISO -> xdelta3")
    xd.add_argument("--no-hardsub", action="store_true",
                    help="diff against the raw (no-hardsub) ISO variant")
    xd.set_defaults(func=cmd_xdelta)

    f = sub.add_parser("full", help="setup + cutscenes + build-iso + xdelta")
    f.add_argument("--translations", nargs="?", const="translations", default=None,
                   metavar="DIR",
                   help="apply edits from <DIR>/*.json (default: 'translations')")
    f.add_argument("--no-hardsub", action="store_true",
                   help="propagated to cutscenes/build-iso/xdelta (raw variant)")
    f.set_defaults(func=cmd_full)
    te = sub.add_parser("translate-extract",
                        help="dump per-file translation catalogs to <DIR>/<ext>/ for editing")
    te.add_argument("--out", default="translations", metavar="DIR",
                    help="catalog output directory (default: 'translations')")
    te.set_defaults(func=cmd_translate_extract)
    tv = sub.add_parser("translate-validate",
                        help="audit JSON catalogs for cap violations, "
                             "latin-1 errors, and edited read-only fields")
    tv.add_argument("--dir", default="translations", metavar="DIR",
                    help="catalog directory to validate (default: 'translations')")
    tv.set_defaults(func=cmd_translate_validate)
    ts = sub.add_parser("translate-status",
                        help="show per-extension translation progress (edited vs untouched)")
    ts.add_argument("--dir", default="translations", metavar="DIR",
                    help="catalog directory (default: 'translations')")
    ts.set_defaults(func=cmd_translate_status)
    dm = sub.add_parser("dump-mkv",
                        help="dump KR/JP cutscenes as MKV (optional --hardsub)")
    dm.add_argument("--regions", default="kr,jp",
                    help="comma-separated subset of {usa,kr,jp} (default: kr,jp)")
    dm.add_argument("--hardsub", action="store_true",
                    help="burn English subs from subs/{korean,japanese}/ into the video")
    dm.add_argument("--jobs", type=int, default=4,
                    help="parallel ffmpeg processes (default: 4)")
    dm.set_defaults(func=cmd_dump_mkv)

    args = ap.parse_args()
    ret: int = args.func(args)
    return ret


if __name__ == "__main__":
    raise SystemExit(main())
