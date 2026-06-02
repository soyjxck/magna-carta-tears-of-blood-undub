"""Cutscene pipeline: source-region (KR or JP) video + audio with English
ASS subtitles burned in.

Two output paths share a common demux + hardsub-encode core:

  * SFD path  (used by `build-iso`)        — re-mux to a SofDec PS the
                                              game's player accepts.
  * MKV path  (used by `dump-mkv`)         — re-mux to Matroska for
                                              previewing outside the game.

Hardsub burn (re-encode video at 5500 kbps CBR with libass) is the same
operation in both paths and lives in `_hardsub_video()`. Slot fit is
not enforced here — the ISO patcher relocates oversize SFDs past the
end of the original ISO.

A small allowlist (`KEEP_USA_CUTSCENES`) names cutscenes where USA's
original SFD bytes are preserved in place rather than re-encoded with
source-region content; these carry baked-in English scrolling text we
want to reuse verbatim in the undub (keeping USA gives us the English
text for free and saves a relocation slot).
"""
from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from sfd_muxer import SFD, SofdecMuxer
from ffmpeg import find_or_build_ffmpeg


# Visual-quality CBR for re-encoded cutscenes. Slot fit is the ISO
# patcher's problem — it relocates oversize files past the original ISO
# end and rewrites the ISO9660 directory entry.
DEFAULT_VIDEO_KBPS = 5500

# Cutscenes where we keep USA's original SFD instead of re-encoding from
# source-region bytes. These carry baked-in English scrolling text we
# want to reuse verbatim in the undub — keeping USA preserves that
# English text exactly, keeps the ISO bytes intact, and saves a
# relocation slot.
KEEP_USA_CUTSCENES = frozenset({
    "180101",  # MOVIE18 — English scrolling text (96.08s)
    "991804",  # MOVIE99 — English scrolling text (41.63s)
})

SUB_DIR_FOR = {"kr": ROOT / "subs" / "korean",
               "jp": ROOT / "subs" / "japanese"}


# --------------------------------------------------------------------------- shared low-level helpers


def _has_dialogue(ass: Path | None) -> bool:
    """True if `ass` exists and contains at least one ``Dialogue:`` line.

    Empty/template `.ass` files (no dialog lines) skip the libass step.
    """
    if ass is None or not ass.exists():
        return False
    return "Dialogue:" in ass.read_text()


def _cache_stamp(job: "CutsceneJob", hardsub: bool) -> dict:
    """Inputs that determine the cached SFD. Cached output is reused only
    when its sidecar stamp equals this dict — covers added/removed/edited
    `.ass`, source SFD changes, and hardsub flag flips."""
    ass_info = None
    if job.has_subs:
        ass_info = {
            "size": job.ass.stat().st_size,
            "sha256": hashlib.sha256(job.ass.read_bytes()).hexdigest(),
        }
    return {
        "version": 1,
        "src_size": job.src_size,
        "src_mtime": int(job.src_sfd.stat().st_mtime),
        "hardsub": bool(hardsub),
        "ass": ass_info,
    }


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-hide_banner", "-v", "error",
         "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)]
    )
    return float(out.decode().strip())


def _demux_sfd_via_ffmpeg(sfd: Path, m1v: Path, sfa: Path, ffmpeg: Path) -> None:
    """Demux SFD into raw MPEG-1 (.m1v) + ADX (.sfa) using ffmpeg."""
    subprocess.run(
        [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(sfd),
         "-map", "0:v", "-c:v", "copy", str(m1v),
         "-map", "0:a", "-c:a", "copy", "-f", "adx", str(sfa)],
        check=True,
    )


def _hardsub_video(m1v_in: Path, m1v_out: Path, ass: Path, ffmpeg: Path,
                   kbps: int = DEFAULT_VIDEO_KBPS) -> None:
    """Re-encode `m1v_in` with `ass` burned in via libass, fixed CBR mpeg1video."""
    subprocess.run(
        [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "warning",
         "-i", str(m1v_in),
         "-vf", f"ass={ass}",
         "-c:v", "mpeg1video",
         "-b:v", f"{kbps}k",
         "-minrate", f"{kbps}k",
         "-maxrate", f"{kbps}k",
         "-bufsize", "1800k",
         "-r", "29.97",
         str(m1v_out)],
        check=True,
    )


# --------------------------------------------------------------------------- SFD pipeline (build-iso)


@dataclass
class CutsceneJob:
    """One cutscene basename across regions: USA original + source-region
    counterpart + an optional `.ass` subtitle file."""
    name: str               # e.g. "181818"
    rel: str                # e.g. "MOVIE18/181818.SFD"
    usa_sfd: Path
    src_sfd: Path
    ass: Path | None
    usa_size: int
    src_size: int
    usa_dur: float
    src_dur: float

    @property
    def has_subs(self) -> bool:
        return _has_dialogue(self.ass)

    @property
    def needs_undub(self) -> bool:
        """True if the SFDs differ between regions (audio swap is meaningful)."""
        return (self.usa_size != self.src_size
                or self.usa_sfd.read_bytes() != self.src_sfd.read_bytes())


def discover_jobs(usa_root: Path = ROOT / "work" / "usa",
                  src_root: Path = ROOT / "work" / "kr",
                  subs_dir: Path = ROOT / "subs" / "korean") -> list[CutsceneJob]:
    """Walk USA's MOVIE18/MOVIE99, pair each SFD with the same-named SFD
    in `src_root`, and pair each with a `<basename>.ass` under `subs_dir`."""
    jobs: list[CutsceneJob] = []
    for sub in ("MOVIE18", "MOVIE99"):
        usa_dir = usa_root / sub
        src_dir = src_root / sub
        if not usa_dir.is_dir():
            continue
        for usa in sorted(usa_dir.glob("*.SFD")):
            src = src_dir / usa.name
            if not src.exists():
                continue
            ass = subs_dir / f"{usa.stem}.ass"
            jobs.append(CutsceneJob(
                name=usa.stem,
                rel=f"{sub}/{usa.name}",
                usa_sfd=usa,
                src_sfd=src,
                ass=ass if ass.exists() else None,
                usa_size=usa.stat().st_size,
                src_size=src.stat().st_size,
                usa_dur=_ffprobe_duration(usa),
                src_dur=_ffprobe_duration(src),
            ))
    return jobs


def build_cutscene(job: CutsceneJob, work_dir: Path, ffmpeg: Path,
                   hardsub: bool = True) -> Path:
    """Produce the undub SFD for one cutscene. Returns the output path.

    Two paths:
      * subs to burn (``hardsub`` and the job has dialog) — demux the
        source SFD, burn the `.ass` in via libass, re-mux to SofDec.
      * no subs — copy the source SFD verbatim. It is CRI's own
        known-good container, so the engine plays the source-region
        audio directly; re-muxing would only strip CRITAGS and drift
        the SCR for no benefit (and trips the muxer on some streams)."""
    wd = work_dir / job.name
    wd.mkdir(parents=True, exist_ok=True)
    out = wd / f"{job.name}_undub.SFD"

    if hardsub and job.has_subs:
        m1v = wd / f"{job.name}.m1v"
        sfa = wd / f"{job.name}.sfa"
        _demux_sfd_via_ffmpeg(job.src_sfd, m1v, sfa, ffmpeg)
        subbed = wd / f"{job.name}_subbed.m1v"
        _hardsub_video(m1v, subbed, job.ass, ffmpeg)
        SofdecMuxer(subbed, sfa).write(out)
    else:
        shutil.copyfile(job.src_sfd, out)
    return out


def run_all(work_dir: Path = ROOT / "build" / "cutscenes-kr",
            src_root: Path = ROOT / "work" / "kr",
            subs_dir: Path = ROOT / "subs" / "korean",
            hardsub: bool = True,
            verbose: bool = True) -> dict[str, Path]:
    """Run the SFD pipeline for every cutscene that needs an undub.

    Skips:
      - cutscenes in `KEEP_USA_CUTSCENES` (USA bytes preserved in place).
      - region-identical SFDs with no work to do (no audio diff and
        either no `.ass` content or hardsub disabled).
      - already-built outputs in `work_dir` (idempotent re-runs).

    With ``hardsub=False`` the libass burn-in is skipped — patched ISO
    cutscenes show the source-region video + audio with no English
    overlay (useful for raw-undub builds for KR/JP readers).

    Returns ``{iso_relative_path: built_sfd_path}`` of every patched cutscene.
    """
    ffmpeg = find_or_build_ffmpeg()
    out: dict[str, Path] = {}
    for job in discover_jobs(src_root=src_root, subs_dir=subs_dir):
        if job.name in KEEP_USA_CUTSCENES:
            if verbose:
                print(f"  [keep USA] {job.rel}  (allow-listed)")
            continue
        wants_subs = hardsub and job.has_subs
        if not (job.needs_undub or wants_subs):
            if verbose:
                reason = "no audio diff + no subs" if not job.has_subs else \
                         "no audio diff + hardsub disabled"
                print(f"  [skip] {job.rel}  ({reason})")
            continue
        target = work_dir / job.name / f"{job.name}_undub.SFD"
        stamp_path = work_dir / job.name / f"{job.name}_undub.stamp.json"
        expected_stamp = _cache_stamp(job, hardsub=hardsub)
        if target.exists() and stamp_path.exists():
            try:
                cached_stamp = json.loads(stamp_path.read_text())
            except Exception:
                cached_stamp = None
            if cached_stamp == expected_stamp:
                if verbose:
                    print(f"  [keep] {job.rel}  -> {target} ({target.stat().st_size:,} B)")
                out["/" + job.rel] = target
                continue
            if verbose:
                print(f"  [stale] {job.rel}  inputs changed, rebuilding")
        if verbose:
            subs_tag = "Y" if wants_subs else "n"
            print(f"  [build] {job.rel}  USA={job.usa_size:,}  src={job.src_size:,}  "
                  f"USA/src dur={job.usa_dur:.1f}/{job.src_dur:.1f}s  subs={subs_tag}")
        try:
            out["/" + job.rel] = build_cutscene(job, work_dir, ffmpeg,
                                                hardsub=hardsub)
            stamp_path.write_text(json.dumps(expected_stamp, indent=2))
        except Exception as e:
            print(f"    ! failed: {e}")
    return out


# --------------------------------------------------------------------------- MKV dump pipeline (dump-mkv)


def _demux_sfd_via_sfd_muxer(sfd_path: Path) -> tuple[bytes, bytes] | None:
    """Demux via the sfd-muxer package (cleaner than ffmpeg's MPEG-PS
    demuxer for SofDec — that one mishandles timestamps and trips
    matroska's write-trailer).

    Returns ``(video_bytes, audio_bytes)`` or None when the SFD has no
    audio stream (silent intro / logo splash — caller falls back to a
    video-only ffmpeg copy).
    """
    try:
        sfd = SFD.from_file(sfd_path)
        return sfd.extract_video(), sfd.extract_audio()
    except Exception as e:
        msg = str(e).lower()
        if "adx" in msg or "sfa" in msg or "audio" in msg:
            return None
        raise


def _mux_to_mkv(m1v: Path, adx: Path | None, out_mkv: Path,
                ffmpeg: Path, source_for_video_only: Path | None = None) -> tuple[bool, str]:
    """Mux video (+ optional ADX audio re-encoded as FLAC) into MKV.

    For video-only SFDs (silent intros) ``source_for_video_only`` is set
    to the original SFD path and we let ffmpeg copy the video stream
    directly — no need to round-trip through sfd-muxer.
    """
    if source_for_video_only is not None:
        cmd = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
               "-fflags", "+genpts", "-i", str(source_for_video_only),
               "-map", "0:v:0", "-c:v", "copy", "-an", str(out_mkv)]
    else:
        cmd = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
               "-fflags", "+genpts",
               "-r", "29.97", "-i", str(m1v),
               "-i", str(adx),
               "-map", "0:v:0", "-c:v", "copy",
               "-map", "1:a:0", "-c:a", "flac", "-compression_level", "5",
               str(out_mkv)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        out_mkv.unlink(missing_ok=True)
        last = r.stderr.strip().splitlines()[-1] if r.stderr else "ffmpeg failed"
        return False, last
    return True, "ok"


def dump_sfd_to_mkv(sfd_path: Path, out_mkv: Path, ass: Path | None,
                    ffmpeg: Path) -> tuple[bool, str]:
    """Dump one SFD to MKV. With `ass` set and containing dialog, English
    subs are burned in via libass before muxing; without it, the source
    video stream is copied verbatim (FLAC audio either way; MKV doesn't
    carry ADX natively).

    Returns ``(ok, message)``. Skips with ``"skip (exists)"`` when the
    output is already on disk and non-empty.
    """
    out_mkv.parent.mkdir(parents=True, exist_ok=True)
    if out_mkv.exists() and out_mkv.stat().st_size > 0:
        return True, "skip (exists)"

    streams = _demux_sfd_via_sfd_muxer(sfd_path)
    if streams is None:
        ok, msg = _mux_to_mkv(m1v=Path("/dev/null"), adx=None, out_mkv=out_mkv,
                              ffmpeg=ffmpeg, source_for_video_only=sfd_path)
        return (ok, "ok (video-only)") if ok else (False, msg)

    video_bytes, audio_bytes = streams
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        m1v = td_p / "v.m1v"
        adx = td_p / "a.adx"
        m1v.write_bytes(video_bytes)
        adx.write_bytes(audio_bytes)

        if _has_dialogue(ass):
            subbed = td_p / "subbed.m1v"
            try:
                _hardsub_video(m1v, subbed, ass, ffmpeg)
            except subprocess.CalledProcessError as e:
                return False, f"hardsub encode failed: {e}"
            ok, msg = _mux_to_mkv(subbed, adx, out_mkv, ffmpeg)
            return (ok, "ok (hardsub)") if ok else (False, msg)
        return _mux_to_mkv(m1v, adx, out_mkv, ffmpeg)


def dump_all_to_mkv(regions: tuple[str, ...] = ("kr", "jp"),
                    hardsub: bool = False,
                    out_root: Path = ROOT / "build" / "cutscene-dumps",
                    jobs: int = 4,
                    verbose: bool = True) -> int:
    """Dump KR and/or JP cutscenes as MKV under
    ``<out_root>/<region>[-hardsub]/<MOVIE18|MOVIE99>/<basename>.mkv``.

    With ``hardsub=True``, English subs from ``subs/{korean,japanese}/``
    are burned into the chosen region's video.
    """
    ffmpeg = find_or_build_ffmpeg()

    queued: list[tuple[Path, Path, Path | None]] = []
    for region in regions:
        if region not in SUB_DIR_FOR:
            if verbose:
                print(f"  [skip] {region}: not a supported source region "
                      f"(use kr or jp)")
            continue
        region_root = ROOT / "work" / region
        if not region_root.is_dir():
            if verbose:
                print(f"  [skip region] {region} not extracted")
            continue
        out_region = out_root / (f"{region}-hardsub" if hardsub else region)
        ass_dir = SUB_DIR_FOR[region] if hardsub else None
        for sub in ("MOVIE18", "MOVIE99"):
            for sfd in sorted((region_root / sub).glob("*.SFD")):
                ass = (ass_dir / f"{sfd.stem}.ass") if ass_dir else None
                out_mkv = out_region / sub / f"{sfd.stem}.mkv"
                queued.append((sfd, out_mkv, ass))

    if verbose:
        kind = "hardsubbed" if hardsub else "raw"
        print(f"queued {len(queued)} cutscene remuxes ({kind}) "
              f"across {len(regions)} regions")
    out_root.mkdir(parents=True, exist_ok=True)

    ok = 0
    failed: list[tuple[Path, str]] = []
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(dump_sfd_to_mkv, sfd, out, ass, ffmpeg): (sfd, out)
                for sfd, out, ass in queued}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            success, msg = fut.result()
            sfd, out_mkv = futs[fut]
            tag = "✓" if success else "✗"
            if verbose:
                rel_in = sfd.relative_to(ROOT / "work")
                rel_out = out_mkv.relative_to(out_root)
                print(f"  [{i:>3}/{len(queued)}] {tag} {rel_in} -> {rel_out}  ({msg})")
            if success:
                ok += 1
            else:
                failed.append((sfd, msg))

    if verbose:
        print(f"\nDone: {ok}/{len(queued)} ok")
        if failed:
            print(f"  {len(failed)} failures:")
            for sfd, msg in failed:
                print(f"    {sfd.name}: {msg}")
    return 0 if not failed else 1


if __name__ == "__main__":
    built = run_all()
    print(f"\nbuilt {len(built)} cutscenes")
