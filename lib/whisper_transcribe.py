"""Whisper wrapper: USA SFD -> SRT (English).

Pipeline:
  1. ffmpeg extracts USA audio to mono 16 kHz WAV (Whisper's preferred format).
  2. openai-whisper transcribes WAV -> segments.
  3. We emit an SRT and a 'work copy' .txt for manual correction.

Defaults to the `small` model — same as the FMA undub. Use --model to change.
For long cutscenes, `medium` improves accuracy at ~3× the runtime.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FFMPEG_LIBASS = ROOT / "work" / "tools" / "ffmpeg-libass" / "bin" / "ffmpeg"


def _ffmpeg() -> str:
    return str(FFMPEG_LIBASS) if FFMPEG_LIBASS.exists() else "ffmpeg"


@dataclass
class Segment:
    start: float
    end: float
    text: str


def extract_wav(sfd: Path, wav_out: Path) -> bool:
    """Extract mono 16 kHz WAV. Returns True on success, False if the SFD has
    no audio stream (silent logo/transition cutscenes do exist)."""
    wav_out.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        [
            _ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(sfd),
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(wav_out),
        ],
        capture_output=True,
    )
    if res.returncode != 0:
        # ffmpeg writes "Output file does not contain any stream" when audio is missing
        if b"does not contain any stream" in (res.stderr or b""):
            return False
        raise subprocess.CalledProcessError(res.returncode, res.args, res.stdout, res.stderr)
    return True


def transcribe(wav: Path, model: str = "small", language: str = "en") -> list[Segment]:
    import whisper  # openai-whisper

    print(f"  loading whisper '{model}' model ...")
    m = whisper.load_model(model)
    print(f"  transcribing {wav.name}")
    result = m.transcribe(str(wav), language=language, verbose=False, word_timestamps=False)
    return [
        Segment(start=float(s["start"]), end=float(s["end"]), text=s["text"].strip())
        for s in result["segments"]
    ]


def _ts(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(segs: list[Segment]) -> str:
    out = []
    for i, s in enumerate(segs, 1):
        out.append(f"{i}\n{_ts(s.start)} --> {_ts(s.end)}\n{s.text}\n")
    return "\n".join(out)


def to_ass(segs: list[Segment], scale: float = 1.0) -> str:
    """Convert segments to an ASS file using the undub default style.

    `scale` lets us compress timestamps proportionally if the KR cutscene is
    shorter than the USA source (KR_duration / USA_duration). Crude but a
    useful first pass — manual correction will still be needed.
    """
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 352
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,28,&H00FFFFFF,&H00000000,&HC0000000,1,0,3,2,2,30,30,18,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header.strip(), ""]

    def ass_ts(t: float) -> str:
        t = t * scale
        h = int(t // 3600)
        m = int((t % 3600) // 60)
        s = t % 60
        return f"{h:01d}:{m:02d}:{s:05.2f}"

    for seg in segs:
        # ASS line breaks are `\N`. Escape commas / curly braces conservatively.
        text = (seg.text.replace("\n", r"\N")
                        .replace("{", "\\{").replace("}", "\\}"))
        lines.append(
            f"Dialogue: 0,{ass_ts(seg.start)},{ass_ts(seg.end)},Default,,0,0,0,,{text}"
        )
    return "\n".join(lines) + "\n"


def transcribe_one(usa_sfd: Path, *, model_obj, language: str = "en",
                   subs_dir: Path = ROOT / "subs",
                   skip_existing: bool = True) -> tuple[Path, Path, int]:
    """One-shot transcribe + write .srt + .ass for a single USA SFD.

    Reuses an already-loaded whisper model object (so the caller can amortise
    load cost across many cutscenes). Returns (srt_path, ass_path, segment_count).
    """
    base = usa_sfd.stem
    out_srt = subs_dir / f"{base}.srt"
    out_ass = subs_dir / f"{base}.ass"
    if skip_existing and out_srt.exists() and out_ass.exists():
        existing = out_srt.read_text().count("\n-->\n")
        return out_srt, out_ass, existing
    out_srt.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        wav = Path(td) / f"{base}.wav"
        if not extract_wav(usa_sfd, wav):
            # silent SFD — write empty stubs so the batch is idempotent
            out_srt.write_text("")
            out_ass.write_text(to_ass([]))
            return out_srt, out_ass, 0
        result = model_obj.transcribe(str(wav), language=language, verbose=False, word_timestamps=False)
        segs = [
            Segment(start=float(s["start"]), end=float(s["end"]), text=s["text"].strip())
            for s in result["segments"]
        ]

    out_srt.write_text(to_srt(segs))
    out_ass.write_text(to_ass(segs))
    return out_srt, out_ass, len(segs)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("usa_sfd", type=Path, nargs="?", help="path to USA SFD (we extract its audio)")
    ap.add_argument("--all", action="store_true", help="batch-transcribe every USA SFD")
    ap.add_argument("--out-srt", type=Path, help="write SRT here (default: subs/<name>.srt)")
    ap.add_argument("--out-ass", type=Path, help="write ASS here (default: subs/<name>.ass)")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="multiplier for timestamps in the ASS (use KR_dur / USA_dur)")
    ap.add_argument("--model", default="small",
                    help="Whisper model (tiny/base/small/medium/large)")
    ap.add_argument("--language", default="en")
    ap.add_argument("--keep-wav", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-transcribe even if .srt+.ass already exist")
    args = ap.parse_args()

    if args.all:
        import whisper as _whisper
        print(f"loading whisper '{args.model}' (one-time) ...")
        m = _whisper.load_model(args.model)
        roots = [Path("work/usa/MOVIE18"), Path("work/usa/MOVIE99")]
        targets = sorted([p for r in roots for p in r.glob("*.SFD")])
        print(f"transcribing {len(targets)} cutscenes ...")
        for i, sfd in enumerate(targets, 1):
            print(f"  [{i:>2}/{len(targets)}] {sfd.name} ...", end=" ", flush=True)
            srt, ass, n = transcribe_one(sfd, model_obj=m, language=args.language,
                                         skip_existing=not args.force)
            print(f"{n} segments -> subs/{sfd.stem}.{{srt,ass}}")
        print(f"\ndone. inspect subs/ and edit any incorrect transcriptions.")
        return 0

    if args.usa_sfd is None:
        ap.error("provide a USA_SFD path or pass --all")

    base = args.usa_sfd.stem
    out_srt = args.out_srt or (ROOT / "subs" / f"{base}.srt")
    out_ass = args.out_ass or (ROOT / "subs" / f"{base}.ass")
    out_srt.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        wav = Path(args.keep_wav and (ROOT / "work" / "scratch" / f"{base}.wav") or Path(td) / f"{base}.wav")
        if args.keep_wav:
            wav.parent.mkdir(parents=True, exist_ok=True)
        print(f"  extracting WAV -> {wav}")
        extract_wav(args.usa_sfd, wav)
        segs = transcribe(wav, model=args.model, language=args.language)

    out_srt.write_text(to_srt(segs))
    out_ass.write_text(to_ass(segs, scale=args.scale))
    print(f"\nwrote {out_srt} ({len(segs)} segments)")
    print(f"wrote {out_ass}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
