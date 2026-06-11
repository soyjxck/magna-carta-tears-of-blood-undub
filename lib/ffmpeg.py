"""Find or build an ffmpeg binary with libass / subtitles filter support.

Mirrors the approach in @soyjxck's fma-broken-angel-undub: search standard
locations first, fall back to building ffmpeg from source into a project-local
cache directory. We don't touch the system ffmpeg.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = ROOT / "work" / "tools" / "ffmpeg-libass"
CACHE_BIN = CACHE_DIR / "bin" / "ffmpeg"
FFMPEG_VERSION = "7.1.1"
BUILD_DIR = ROOT / "work" / "tools" / "ffmpeg-build"


def _has_libass(ffmpeg: str | Path) -> bool:
    try:
        out = subprocess.check_output(
            [str(ffmpeg), "-hide_banner", "-filters"], stderr=subprocess.DEVNULL
        ).decode("utf-8", "replace")
    except Exception:
        return False
    return "subtitles" in out and "ass " in out


def _candidates() -> list[Path]:
    paths: list[Path] = [CACHE_BIN]
    sys_path = shutil.which("ffmpeg")
    if sys_path:
        paths.append(Path(sys_path))
    for cand in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
        paths.append(Path(cand))
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        if p in seen or not p.exists():
            continue
        seen.add(p)
        out.append(p)
    return out


def find_ffmpeg_libass() -> Path | None:
    for p in _candidates():
        if _has_libass(p):
            return p
    return None


def build_ffmpeg_libass() -> Path:
    """Build ffmpeg with --enable-libass into work/tools/ffmpeg-libass/."""
    if platform.system() != "Darwin":
        sys.exit(
            "build_ffmpeg_libass: only macOS auto-build is implemented. "
            "On Linux, install ffmpeg with libass via your package manager "
            "(apt: ffmpeg / dnf: ffmpeg) and place it on PATH."
        )
    # Homebrew dep check (don't auto-install; ask the user to brew install if missing).
    needed = ["libass", "x264", "pkgconf", "freetype", "fontconfig", "harfbuzz"]
    missing = [d for d in needed if not _brew_installed(d)]
    if missing:
        sys.exit(
            "missing Homebrew dependencies for ffmpeg+libass: "
            + " ".join(missing)
            + f"\n  brew install {' '.join(missing)}"
        )

    src_tar = BUILD_DIR / f"ffmpeg-{FFMPEG_VERSION}.tar.xz"
    src_dir = BUILD_DIR / f"ffmpeg-{FFMPEG_VERSION}"
    BUILD_DIR.mkdir(parents=True, exist_ok=True)

    if not src_tar.exists():
        url = f"https://ffmpeg.org/releases/ffmpeg-{FFMPEG_VERSION}.tar.xz"
        print(f"  downloading {url}")
        subprocess.run(["curl", "-fsSL", "-o", str(src_tar), url], check=True)
    if not src_dir.exists():
        print(f"  extracting {src_tar.name}")
        subprocess.run(["tar", "-C", str(BUILD_DIR), "-xf", str(src_tar)], check=True)

    brew_prefix = subprocess.check_output(["brew", "--prefix"]).decode().strip()
    extra_cflags = f"-I{brew_prefix}/include"
    extra_ldflags = f"-L{brew_prefix}/lib"
    pkg_path = f"{brew_prefix}/lib/pkgconfig:{brew_prefix}/share/pkgconfig"
    env = {**os.environ, "PKG_CONFIG_PATH": pkg_path}

    if not CACHE_BIN.exists():
        print(f"  configuring ffmpeg-{FFMPEG_VERSION}")
        configure = [
            "./configure",
            f"--prefix={CACHE_DIR}",
            "--enable-gpl",
            "--enable-libass",
            "--enable-libx264",
            "--enable-libfreetype",
            "--enable-libfontconfig",
            "--enable-libharfbuzz",
            "--enable-videotoolbox",
            "--enable-audiotoolbox",
            f"--extra-cflags={extra_cflags}",
            f"--extra-ldflags={extra_ldflags}",
        ]
        subprocess.run(configure, cwd=src_dir, env=env, check=True)
        cores = os.cpu_count() or 4
        print(f"  building (make -j{cores}) — takes a few minutes")
        subprocess.run(["make", f"-j{cores}"], cwd=src_dir, env=env, check=True)
        subprocess.run(["make", "install"], cwd=src_dir, env=env, check=True)

    if not _has_libass(CACHE_BIN):
        sys.exit(f"built ffmpeg lacks libass: {CACHE_BIN}")
    return CACHE_BIN


def _brew_installed(formula: str) -> bool:
    try:
        subprocess.run(
            ["brew", "list", "--versions", formula],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        return False


def find_or_build_ffmpeg() -> Path:
    found = find_ffmpeg_libass()
    if found is not None:
        return found
    print("no ffmpeg with libass found — building from source")
    return build_ffmpeg_libass()


if __name__ == "__main__":
    print(find_or_build_ffmpeg())
