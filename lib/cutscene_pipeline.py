"""Per-cutscene undub: KR video + KR audio with English ASS burned in.

For one SFD basename (e.g. "181818"):
  1. Demux KR/<name>.SFD -> .m1v + .sfa via ffmpeg-libass
  2. Compute CBR video bitrate so the muxed result fits in USA's slot
  3. Re-encode .m1v with `ass=subs/<name>.ass` filter at that CBR
  4. Mux re-encoded video + KR audio via lib/sofdec_mux.SofdecMuxer
  5. Return the final .SFD path

Skips a cutscene when:
  - USA and KR SFDs are byte-identical (no audio change worth doing) AND
    there is no .ass file with content (nothing to burn).
  - The KR cutscene is so much longer than USA that no reasonable CBR fits
    (we surface this so the user sees the trade-off).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from sofdec_mux import SofdecMuxer
from ffmpeg_libass import find_or_build_ffmpeg


ADX_AUDIO_BPS = 432_000
# Fixed visual-quality CBR for re-encoded cutscenes. We don't constrain to the
# original USA slot size — the ISO patcher relocates oversize files past the
# end of the original ISO and patches the ISO9660 directory entry to match.
DEFAULT_VIDEO_KBPS = 5500


@dataclass
class CutsceneJob:
    name: str               # e.g. "181818"
    rel: str                # e.g. "MOVIE18/181818.SFD"
    usa_sfd: Path
    kr_sfd: Path
    ass: Path | None        # None if no subs file or it's empty
    usa_size: int
    kr_size: int
    usa_dur: float
    kr_dur: float

    @property
    def has_subs(self) -> bool:
        if self.ass is None or not self.ass.exists():
            return False
        text = self.ass.read_text()
        return "Dialogue:" in text

    @property
    def needs_undub(self) -> bool:
        """True if the SFDs differ between regions (audio swap is meaningful)."""
        return self.usa_size != self.kr_size or self.usa_sfd.read_bytes() != self.kr_sfd.read_bytes()


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.check_output(
        ["ffprobe", "-hide_banner", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)]
    )
    return float(out.decode().strip())


def discover_jobs(usa_root: Path = ROOT / "work" / "usa",
                  kr_root: Path = ROOT / "work" / "kr",
                  subs_dir: Path = ROOT / "subs") -> list[CutsceneJob]:
    jobs: list[CutsceneJob] = []
    for sub in ("MOVIE18", "MOVIE99"):
        usa_dir = usa_root / sub
        kr_dir = kr_root / sub
        if not usa_dir.is_dir():
            continue
        for usa in sorted(usa_dir.glob("*.SFD")):
            kr = kr_dir / usa.name
            if not kr.exists():
                continue
            ass = subs_dir / f"{usa.stem}.ass"
            jobs.append(CutsceneJob(
                name=usa.stem,
                rel=f"{sub}/{usa.name}",
                usa_sfd=usa,
                kr_sfd=kr,
                ass=ass if ass.exists() else None,
                usa_size=usa.stat().st_size,
                kr_size=kr.stat().st_size,
                usa_dur=_ffprobe_duration(usa),
                kr_dur=_ffprobe_duration(kr),
            ))
    return jobs


def _video_bitrate_kbps(usa_size: int, kr_dur: float) -> int:
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
         "-i", str(job.kr_sfd),
         "-map", "0:v", "-c:v", "copy", str(m1v),
         "-map", "0:a", "-c:a", "copy", "-f", "adx", str(sfa)],
        check=True,
    )

    # 2) re-encode video — with subs if we have any, otherwise stream-copy.
    bitrate_kbps = _video_bitrate_kbps(job.usa_size, job.kr_dur)
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


def run_all(work_dir: Path = ROOT / "build" / "cutscenes",
            verbose: bool = True) -> dict[str, Path]:
    """Run the pipeline for every cutscene that needs an undub.

    Skips:
      - region-identical SFDs with no .ass content (no work to do)
      - already-built outputs in `work_dir` (idempotent re-runs)
    Returns {iso_relative_path: output_sfd_path} of every patched cutscene.
    """
    ffmpeg = find_or_build_ffmpeg()
    out: dict[str, Path] = {}
    jobs = discover_jobs()
    for job in jobs:
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
            print(f"  [build] {job.rel}  USA={job.usa_size:,}  KR={job.kr_size:,}  "
                  f"USA/KR dur={job.usa_dur:.1f}/{job.kr_dur:.1f}s  subs={'Y' if wants_sub_burn else 'n'}")
        try:
            built = build_cutscene(job, work_dir, ffmpeg)
        except Exception as e:
            print(f"    ! failed: {e}")
            continue
        out["/" + job.rel] = built
    return out


if __name__ == "__main__":
    built = run_all()
    print(f"\nbuilt {len(built)} cutscenes")
