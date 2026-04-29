"""Per-cutscene undub: source-region (KR or JP) video + audio with
English ASS subtitles burned in.

For one SFD basename (e.g. "181818"):
  1. Demux <source>/<name>.SFD -> .m1v + .sfa via ffmpeg-libass
  2. Re-encode .m1v with `ass=subs/<source>/<name>.ass` filter at a
     fixed CBR (slot fit is handled later by the ISO patcher, which
     relocates oversize files past the original ISO end)
  3. Mux re-encoded video + source audio via the sfd-muxer package
  4. Return the final .SFD path

Skips a cutscene when:
  - USA and source SFDs are byte-identical (no audio change worth doing)
    AND there is no .ass file with content (nothing to burn).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from sfd_muxer import SofdecMuxer
from ffmpeg import find_or_build_ffmpeg


ADX_AUDIO_BPS = 432_000
# Fixed visual-quality CBR for re-encoded cutscenes. We don't constrain to the
# original USA slot size — the ISO patcher relocates oversize files past the
# end of the original ISO and patches the ISO9660 directory entry to match.
DEFAULT_VIDEO_KBPS = 5500

# Cutscenes where we explicitly keep USA's original SFD instead of swapping
# in a source-region re-encode. These are silent / no-dialog transitions
# where the source-region byte differences don't change anything visible
# or audible — using USA preserves the original ISO bytes exactly and
# saves a relocation slot.
KEEP_USA_CUTSCENES = frozenset({
    "180101",  # MOVIE18 — silent transition (96.08s, all regions match duration)
    "991804",  # MOVIE99 — silent transition (41.63s, all regions match duration)
})


@dataclass
class CutsceneJob:
    name: str               # e.g. "181818"
    rel: str                # e.g. "MOVIE18/181818.SFD"
    usa_sfd: Path
    src_sfd: Path
    ass: Path | None        # None if no subs file or it's empty
    usa_size: int
    src_size: int
    usa_dur: float
    src_dur: float

    @property
    def has_subs(self) -> bool:
        if self.ass is None or not self.ass.exists():
            return False
        text = self.ass.read_text()
        return "Dialogue:" in text

    @property
    def needs_undub(self) -> bool:
        """True if the SFDs differ between regions (audio swap is meaningful)."""
        return self.usa_size != self.src_size or self.usa_sfd.read_bytes() != self.src_sfd.read_bytes()


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-hide_banner", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)]
    )
    return float(out.decode().strip())


def discover_jobs(usa_root: Path = ROOT / "work" / "usa",
                  src_root: Path = ROOT / "work" / "kr",
                  subs_dir: Path = ROOT / "subs" / "korean") -> list[CutsceneJob]:
    """Walk USA's MOVIE18/MOVIE99, pair each SFD with the same-named SFD in
    `src_root` (the source region to take video+audio from), and pair each
    with a `<basename>.ass` subtitle file under `subs_dir` if present."""
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


def _video_bitrate_kbps(usa_size: int, src_dur: float) -> int:
    """Always returns the project-wide default. Slot fit is handled later by
    the ISO patcher (which relocates oversize files past the original ISO end)."""
    return DEFAULT_VIDEO_KBPS


def build_cutscene(job: CutsceneJob, work_dir: Path, ffmpeg: Path) -> Path:
    """Run the full per-cutscene pipeline. Returns the muxed output path."""
    wd = work_dir / job.name
    wd.mkdir(parents=True, exist_ok=True)
    m1v = wd / f"{job.name}.m1v"
    sfa = wd / f"{job.name}.sfa"
    subbed = wd / f"{job.name}_subbed.m1v"
    out = wd / f"{job.name}_undub.SFD"

    # 1) demux KR
    subprocess.run(
        [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(job.src_sfd),
         "-map", "0:v", "-c:v", "copy", str(m1v),
         "-map", "0:a", "-c:a", "copy", "-f", "adx", str(sfa)],
        check=True,
    )

    # 2) re-encode video — with subs if we have any, otherwise stream-copy.
    bitrate_kbps = _video_bitrate_kbps(job.usa_size, job.src_dur)
    if job.has_subs:
        subprocess.run(
            [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "warning",
             "-i", str(m1v),
             "-vf", f"ass={job.ass}",
             "-c:v", "mpeg1video",
             "-b:v", f"{bitrate_kbps}k",
             "-minrate", f"{bitrate_kbps}k",
             "-maxrate", f"{bitrate_kbps}k",
             "-bufsize", "1800k",
             "-r", "29.97",
             str(subbed)],
            check=True,
        )
        video_for_mux = subbed
    else:
        # No subs to burn — just keep the KR video stream as-is. We still
        # have to re-mux because we need to pair it with the KR audio in a
        # SofDec PS that the game's player accepts.
        video_for_mux = m1v

    # 3) mux video + audio. Output may be larger than the USA slot — that's
    # fine; the ISO patcher will relocate it past the original ISO end and
    # patch the ISO9660 directory entry.
    SofdecMuxer(video_for_mux, sfa).write(out)
    return out


def run_all(work_dir: Path = ROOT / "build" / "cutscenes-kr",
            src_root: Path = ROOT / "work" / "kr",
            subs_dir: Path = ROOT / "subs" / "korean",
            verbose: bool = True) -> dict[str, Path]:
    """Run the pipeline for every cutscene that needs an undub.

    Skips:
      - region-identical SFDs with no .ass content (no work to do)
      - already-built outputs in `work_dir` (idempotent re-runs)
    Returns {iso_relative_path: output_sfd_path} of every patched cutscene.
    """
    ffmpeg = find_or_build_ffmpeg()
    out: dict[str, Path] = {}
    jobs = discover_jobs(src_root=src_root, subs_dir=subs_dir)
    for job in jobs:
        if job.name in KEEP_USA_CUTSCENES:
            if verbose:
                print(f"  [keep USA] {job.rel}  (allow-listed)")
            continue
        wants_audio_swap = job.needs_undub
        wants_sub_burn = job.has_subs
        if not (wants_audio_swap or wants_sub_burn):
            if verbose:
                print(f"  [skip] {job.rel}  (no audio diff + no subs)")
            continue
        target = work_dir / job.name / f"{job.name}_undub.SFD"
        if target.exists():
            if verbose:
                print(f"  [keep] {job.rel}  -> {target} ({target.stat().st_size:,} B)")
            out["/" + job.rel] = target
            continue
        if verbose:
            print(f"  [build] {job.rel}  USA={job.usa_size:,}  src={job.src_size:,}  "
                  f"USA/src dur={job.usa_dur:.1f}/{job.src_dur:.1f}s  subs={'Y' if wants_sub_burn else 'n'}")
        try:
            built = build_cutscene(job, work_dir, ffmpeg)
        except Exception as e:
            print(f"    ! failed: {e}")
            continue
        out["/" + job.rel] = built
    return out


# --------------------------------------------------------------------------- MKV dump

def dump_sfd_to_mkv(sfd_path: Path, out_mkv: Path,
                    ass: Path | None, ffmpeg: Path,
                    bitrate_kbps: int = DEFAULT_VIDEO_KBPS) -> tuple[bool, str]:
    """Dump one SFD to MKV. With ``ass`` set, English subs are burned
    into the video via libass before muxing (re-encoded mpeg1video CBR);
    without it, the source video stream is copied verbatim.

    Returns ``(ok, message)``. Skips with ``"skip (exists)"`` when the
    output is already on disk and non-empty.
    """
    import tempfile
    from sfd_muxer import SFD

    out_mkv.parent.mkdir(parents=True, exist_ok=True)
    if out_mkv.exists() and out_mkv.stat().st_size > 0:
        return True, "skip (exists)"

    # Use sfd-muxer to demux: ffmpeg's MPEG-PS demuxer mishandles SofDec
    # timestamps and trips the matroska muxer's write-trailer. Going
    # through clean elementary streams sidesteps that whole class of bug.
    video_only = False
    try:
        sfd = SFD.from_file(sfd_path)
        video_bytes = sfd.extract_video()
        audio_bytes = sfd.extract_audio()
    except Exception as e:
        msg = str(e)
        if "ADX" in msg or "SFA" in msg or "audio" in msg.lower():
            video_only = True   # silent intros/logos: no audio stream
            video_bytes = audio_bytes = b""
        else:
            return False, f"sfd demux failed: {e}"

    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)

        if video_only:
            # Source had no audio — let ffmpeg's PS demuxer copy the video
            # stream directly (faster than going through sfd-muxer for the
            # video-only edge case).
            cmd = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                   "-fflags", "+genpts", "-i", str(sfd_path),
                   "-map", "0:v:0", "-c:v", "copy", "-an", str(out_mkv)]
        else:
            m1v = td_p / "v.m1v"
            adx = td_p / "a.adx"
            m1v.write_bytes(video_bytes)
            adx.write_bytes(audio_bytes)

            video_for_mux = m1v
            if ass is not None and ass.exists() and "Dialogue:" in ass.read_text():
                # Re-encode video with hardsubbed .ass overlay
                subbed = td_p / "subbed.m1v"
                r = subprocess.run(
                    [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "warning",
                     "-r", "29.97", "-i", str(m1v),
                     "-vf", f"ass={ass}",
                     "-c:v", "mpeg1video",
                     "-b:v", f"{bitrate_kbps}k",
                     "-minrate", f"{bitrate_kbps}k",
                     "-maxrate", f"{bitrate_kbps}k",
                     "-bufsize", "1800k",
                     "-r", "29.97", str(subbed)],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    return False, f"hardsub encode failed: {r.stderr.strip().splitlines()[-1] if r.stderr else 'ffmpeg failed'}"
                video_for_mux = subbed

            # MKV doesn't carry ADX natively; encode audio to FLAC (lossless
            # from the decoded ADX PCM).
            cmd = [str(ffmpeg), "-y", "-hide_banner", "-loglevel", "error",
                   "-fflags", "+genpts",
                   "-r", "29.97", "-i", str(video_for_mux),
                   "-i", str(adx),
                   "-map", "0:v:0", "-c:v", "copy",
                   "-map", "1:a:0", "-c:a", "flac", "-compression_level", "5",
                   str(out_mkv)]

        r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        out_mkv.unlink(missing_ok=True)
        last = r.stderr.strip().splitlines()[-1] if r.stderr else "ffmpeg failed"
        return False, last
    if video_only:
        msg = "ok (video-only)"
    elif ass is not None and ass.exists() and "Dialogue:" in ass.read_text():
        msg = "ok (hardsub)"
    else:
        msg = "ok"
    return True, msg


SUB_DIR_FOR = {"kr": ROOT / "subs" / "korean",
               "jp": ROOT / "subs" / "japanese"}


def dump_all_to_mkv(regions: tuple[str, ...] = ("kr", "jp"),
                    hardsub: bool = False,
                    out_root: Path = ROOT / "build" / "cutscene-dumps",
                    jobs: int = 4,
                    verbose: bool = True) -> int:
    """Dump cutscenes from KR and/or JP as MKV under
    ``<out_root>/<region>[-hardsub]/<MOVIE18|MOVIE99>/<basename>.mkv``.

    With ``hardsub=True``, English subs from ``subs/{korean,japanese}/``
    are burned into the chosen region's video.
    """
    import concurrent.futures as cf

    ffmpeg = find_or_build_ffmpeg()

    queued: list[tuple[Path, Path, Path | None]] = []
    for region in regions:
        if region not in SUB_DIR_FOR:
            if verbose:
                print(f"  [skip] {region} not a supported source region "
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
        print(f"queued {len(queued)} cutscene remuxes "
              f"({'hardsubbed' if hardsub else 'raw'}) across {len(regions)} regions")
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
