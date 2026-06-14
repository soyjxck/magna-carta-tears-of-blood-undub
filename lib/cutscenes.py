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
from typing import TypeGuard

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from ffmpeg import find_or_build_ffmpeg
from sfd_muxer import SFD, SofdecMuxer

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

# Cutscene(s) where we keep USA's *video* (its baked-in English credit
# roll) but mux the source-region *audio* underneath, burning subs on top
# when an `.ass` exists. The ending credits (189992) want the English
# credit names on screen plus the original-language ending song. When the
# USA roll is shorter than the source song (e.g. JP's 189992 is a longer
# roll), build_cutscene time-stretches the USA video to the source song's
# length so the roll and song finish together — no freeze, no cut.
AUDIO_SWAP_CUTSCENES = frozenset({
    "189992",  # ending credits — USA English roll + source song (+ lyric subs if present)
})

SUB_DIR_FOR = {"kr": ROOT / "subs" / "korean",
               "jp": ROOT / "subs" / "japanese"}

# Regions that can be dumped to MKV. USA is the English base (already in
# English, no subtitle overlay to burn); kr/jp are the source regions and
# can optionally carry hardsubs from SUB_DIR_FOR.
DUMPABLE_REGIONS = ("usa", "kr", "jp")


# ------------------------------------------------------------------------- shared low-level helpers


def _has_dialogue(ass: Path | None) -> TypeGuard[Path]:
    """True if `ass` exists and contains at least one ``Dialogue:`` line.

    Empty/template `.ass` files (no dialog lines) skip the libass step.
    """
    if ass is None or not ass.exists():
        return False
    return "Dialogue:" in ass.read_text()


def _cache_stamp(job: CutsceneJob, hardsub: bool) -> dict[str, object]:
    """Inputs that determine the cached SFD. Cached output is reused only
    when its sidecar stamp equals this dict — covers added/removed/edited
    `.ass`, source SFD changes, and hardsub flag flips."""
    ass_info = None
    if job.has_subs:
        assert job.ass is not None  # has_subs ⇒ an .ass file exists
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


def _ffprobe_for(ffmpeg: Path) -> str:
    """The ffprobe that ships beside the located/built ffmpeg. Using it keeps
    us off a system ffprobe — the whole point of find_or_build_ffmpeg is to not
    depend on one. Falls back to PATH only if no sibling exists."""
    sibling = ffmpeg.parent / "ffprobe"
    return str(sibling) if sibling.exists() else "ffprobe"


def _ffprobe_duration(path: Path, ffprobe: str = "ffprobe") -> float:
    out = subprocess.check_output(
        [ffprobe, "-hide_banner", "-v", "error",
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


def _hardsub_video(m1v_in: Path, m1v_out: Path, ass: Path | None, ffmpeg: Path,
                   kbps: int = DEFAULT_VIDEO_KBPS, stretch: float = 1.0) -> None:
    """Re-encode `m1v_in` as fixed-CBR mpeg1video. Burns `ass` via libass when
    given, and time-stretches the picture by `stretch` (a PTS factor > 1 slows
    it down) so an English credit roll can be matched to a longer source song."""
    filters = []
    if abs(stretch - 1.0) > 1e-3:
        filters.append(f"setpts=PTS*{stretch:.6f}")
    if ass is not None:
        filters.append(f"ass={ass}")
    vf = ",".join(filters) if filters else "null"
    subprocess.run(
        [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "warning",
         "-i", str(m1v_in),
         "-vf", vf,
         "-c:v", "mpeg1video",
         "-b:v", f"{kbps}k",
         "-minrate", f"{kbps}k",
         "-maxrate", f"{kbps}k",
         "-bufsize", "1800k",
         "-r", "29.97",
         str(m1v_out)],
        check=True,
    )


# ------------------------------------------------------------------------- SFD pipeline (build-iso)


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
                  subs_dir: Path = ROOT / "subs" / "korean",
                  ffprobe: str = "ffprobe") -> list[CutsceneJob]:
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
                usa_dur=_ffprobe_duration(usa, ffprobe),
                src_dur=_ffprobe_duration(src, ffprobe),
            ))
    return jobs


def build_cutscene(job: CutsceneJob, work_dir: Path, ffmpeg: Path,
                   hardsub: bool = True) -> Path:
    """Produce the undub SFD for one cutscene. Returns the output path.

    Three paths:
      * audio swap (name in ``AUDIO_SWAP_CUTSCENES``) — keep USA's video
        for its baked-in English text, take the source-region audio, burn
        the `.ass` in when one exists, time-stretch the video to the source
        song's length when the durations differ, then re-mux to SofDec.
      * subs to burn (``hardsub`` and the job has dialog) — demux the
        source SFD, burn the `.ass` in via libass, re-mux to SofDec.
      * no subs — copy the source SFD verbatim. It is CRI's own
        known-good container, so the engine plays the source-region
        audio directly; re-muxing would only strip CRITAGS and drift
        the SCR for no benefit (and trips the muxer on some streams)."""
    wd = work_dir / job.name
    wd.mkdir(parents=True, exist_ok=True)
    out = wd / f"{job.name}_undub.SFD"
    # Build into a temp name and rename only on success — build-iso ships any
    # *_undub.SFD that exists, so a crash mid-write must never leave a
    # truncated file under the final name.
    tmp = wd / f"{job.name}_undub.part.SFD"

    try:
        # Audio-swap scenes keep USA's English video over the source-region
        # audio even with no subs (e.g. JP credits). When USA's video is
        # shorter than the source song, stretch it to match so the roll and
        # song finish together instead of drifting/cutting.
        do_swap = hardsub and job.name in AUDIO_SWAP_CUTSCENES
        if hardsub and (job.has_subs or do_swap):
            m1v = wd / f"{job.name}.m1v"
            sfa = wd / f"{job.name}.sfa"
            stretch = 1.0
            if do_swap:
                _demux_sfd_via_ffmpeg(job.usa_sfd, m1v, wd / f"{job.name}.usa.sfa", ffmpeg)
                _demux_sfd_via_ffmpeg(job.src_sfd, wd / f"{job.name}.src.m1v", sfa, ffmpeg)
                if job.usa_dur and abs(job.usa_dur - job.src_dur) > 1.0:
                    stretch = job.src_dur / job.usa_dur
            else:
                _demux_sfd_via_ffmpeg(job.src_sfd, m1v, sfa, ffmpeg)
            subbed = wd / f"{job.name}_subbed.m1v"
            _hardsub_video(m1v, subbed, job.ass if job.has_subs else None,
                           ffmpeg, stretch=stretch)
            SofdecMuxer(subbed, sfa).write(tmp)
        else:
            shutil.copyfile(job.src_sfd, tmp)
        tmp.replace(out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
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
    ffprobe = _ffprobe_for(ffmpeg)
    out: dict[str, Path] = {}
    failed: list[tuple[str, str]] = []
    for job in discover_jobs(src_root=src_root, subs_dir=subs_dir, ffprobe=ffprobe):
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
            failed.append((job.rel, str(e)))
            print(f"    ! failed: {e}")
    if failed:
        detail = "\n".join(f"    {rel}: {msg}" for rel, msg in failed)
        raise RuntimeError(
            f"{len(failed)} of {len(failed) + len(out)} cutscene(s) failed to "
            f"build:\n{detail}\nThe ISO would otherwise ship these scenes with "
            f"USA (English) audio — fix the inputs and re-run."
        )
    return out


# --------------------------------------------------------------------- MKV dump pipeline (dump-mkv)


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


def dump_all_to_mkv(regions: tuple[str, ...] = ("usa", "kr", "jp"),
                    patched: bool = False,
                    out_root: Path = ROOT / "build" / "cutscene-dumps",
                    jobs: int = 4,
                    verbose: bool = True) -> int:
    """Dump cutscenes to MKV for review, in one of two modes.

    raw (default)
        Each region's ORIGINAL cutscenes, straight from ``work/<region>``
        (usa/kr/jp), with no subtitles — a faithful rip of the untouched
        source ISOs. Output: ``<out_root>/<region>/``.

    patched (``patched=True``)
        The UNDUB cutscenes that actually ship in the patched ISO,
        reconstructed from the build outputs: per scene, the built undub
        SFD (``build/cutscenes-<region>/<name>/<name>_undub.SFD``) when one
        exists, else USA's original SFD (the slots ``build-iso`` leaves
        untouched). English subs and the English-credit swap/stretch are
        already baked into those SFDs, so this is a pure remux — it can't
        drift from the ISO. Only the source regions (kr/jp) have patched
        builds. Output: ``<out_root>/<region>-patched/``.
    """
    ffmpeg = find_or_build_ffmpeg()
    usa_root = ROOT / "work" / "usa"

    queued: list[tuple[Path, Path]] = []
    for region in regions:
        if region not in DUMPABLE_REGIONS:
            if verbose:
                print(f"  [skip] {region}: not a dumpable region (use usa, kr, or jp)")
            continue
        if patched:
            if region == "usa":
                if verbose:
                    print("  [skip] usa has no patched build (it is the English base)")
                continue
            built_dir = ROOT / "build" / f"cutscenes-{region}"
            if not built_dir.is_dir():
                if verbose:
                    print(f"  [skip region] {region}: no patched build — "
                          f"run `patch.py --source {region} cutscenes` first")
                continue
            out_region = out_root / f"{region}-patched"
            for sub in ("MOVIE18", "MOVIE99"):
                for usa_sfd in sorted((usa_root / sub).glob("*.SFD")):
                    name = usa_sfd.stem
                    built = built_dir / name / f"{name}_undub.SFD"
                    src = built if built.exists() else usa_sfd
                    queued.append((src, out_region / sub / f"{name}.mkv"))
        else:
            region_root = ROOT / "work" / region
            if not region_root.is_dir():
                if verbose:
                    print(f"  [skip region] {region} not extracted")
                continue
            out_region = out_root / region
            for sub in ("MOVIE18", "MOVIE99"):
                for sfd in sorted((region_root / sub).glob("*.SFD")):
                    queued.append((sfd, out_region / sub / f"{sfd.stem}.mkv"))

    if verbose:
        kind = "patched undub" if patched else "raw original"
        print(f"queued {len(queued)} cutscene remuxes ({kind})")
    out_root.mkdir(parents=True, exist_ok=True)

    ok = 0
    failed: list[tuple[Path, str]] = []
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(dump_sfd_to_mkv, sfd, out, None, ffmpeg): (sfd, out)
                for sfd, out in queued}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            success, msg = fut.result()
            sfd, out_mkv = futs[fut]
            tag = "✓" if success else "✗"
            if verbose:
                print(f"  [{i:>3}/{len(queued)}] {tag} {out_mkv.relative_to(out_root)}  ({msg})")
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
