# Magna Carta: Tears of Blood — Undub Patch

Restores the original Korean voice acting in the USA PS2 release of *Magna Carta: Tears of Blood* (SLUS-21221). Cutscenes are re-encoded with Korean audio + English subtitles burned into the video; in-game dialog runs from KR's audio archive while the UI / item / dialog text stays English.

If this helped you, consider [buying me a coffee](https://ko-fi.com/soyjack)

## What's Changed

| Content | Status |
|---|---|
| Cutscene voice (39 SFDs) | Korean audio + English subtitles burned into video |
| In-game character voice | Korean (full KR `MUSIC.AFS` swapped in) |
| Battle voices, NPC barks | Korean |
| World NPC dialog (`.fpb`, 707 files) | English text on Korean voice |
| Phone conversations + option dialogs (`.cht`) | English |
| UI labels, item names, abilities, monster bestiary, talisman descriptions | English |
| Engine messages, menus | English (USA boot ELF + USA fonts kept) |
| Music + SFX | Unchanged |

## How to Patch

### Option 1 — xdelta (recommended)

Pre-built patch. No build tools needed.

**Requirements**: USA ISO + [DeltaPatcher](https://github.com/marco-calautti/DeltaPatcher/releases)

1. Download `magna-carta-tears-of-blood-undub.xdelta` from [Releases](https://github.com/soyjxck/magna-carta-undub/releases/latest)
2. Open DeltaPatcher
3. **Original file**: `Magna Carta - Tears of Blood (USA).iso`
4. **Patch file**: `magna-carta-tears-of-blood-undub.xdelta`
5. Click **Apply patch**

```bash
# Or via command line:
xdelta3 -d -s "Magna Carta - Tears of Blood (USA).iso" magna-carta-tears-of-blood-undub.xdelta out.iso
```

### Option 2 — Full pipeline (rebuild from sources)

Build from both ISOs. Auto-compiles ffmpeg with subtitle support on first run.

**Requirements**: Python 3.11+, both ISOs (USA + KR), `7z`, `xdelta3`

```bash
git clone https://github.com/soyjxck/magna-carta-undub.git
cd magna-carta-undub
mkdir -p roms
# place both ISOs in roms/:
#   roms/Magna Carta - Tears of Blood (USA).iso
#   roms/Magna Carta - Jinhongui Seongheun (Korea).iso
pip install -r requirements.txt
python3 patch.py full
```

Or run each phase explicitly:

```bash
python3 patch.py setup        # extract both ISOs, auto-build ffmpeg+libass
python3 patch.py cutscenes    # demux/re-encode/mux all SFDs with EN subs (subs/*.ass) burned in
python3 patch.py build-iso    # apply Phase 1 (cutscenes) + Phase 3 (KR LINEAR/MUSIC + hybrid SHIP) -> build/magna-carta-tears-of-blood-undub.iso
python3 patch.py xdelta       # generate build/magna-carta-tears-of-blood-undub.xdelta
```

The `subs/` directory ships with the English `.ass` subtitle files used for cutscene burn-in. They were transcribed from the USA audio once and committed to the repo; rebuilding from scratch doesn't re-run any transcription.

## How It Works (TL;DR)

The game is built on Unreal Engine 2 with CRI's AFS archive format and SofDec MPEG-PS cutscenes. Korean voice IDs and USA voice IDs occupy completely disjoint ID ranges (no 1:1 ID mapping), and the engine's `MrtsGame.u` bytecode is tightly coupled to whichever region's data files it ships with. So a USA-base voice swap doesn't work — we instead use **KR LINEAR.AFS + KR MUSIC.AFS + KR's scene scripts in SHIP.AFS** (so KR scripts call KR voice IDs that exist in KR audio), then **overlay USA bytes for text-bearing files** in SHIP (`.cht`, `.fpb`, `.tui`, `.itm`, etc.) so visible text stays English.

The keystone discovery: SHIP.AFS slot 0 is a plaintext `(filename, decimal-size)` manifest the engine reads at boot to populate its file-size cache. When we swap USA bytes in but leave the manifest pointing at KR sizes, the engine reads short and the parser overruns its buffer. Rebuilding the manifest with the hybrid's actual sizes is the fix that unlocks full English coverage on the world dialog (`.fpb`).

Cutscenes use [`sfd-muxer`](https://github.com/soyjxck/sfd-muxer) — we demux KR SFDs, re-encode video at 5500 kbps CBR with English subtitles (pre-shipped in `subs/`) burned in via libass, then mux back to a fresh SFD.

Full reverse-engineering record in [TECHNICAL.md](TECHNICAL.md).

## Repo Layout

```
patch.py                  # CLI — setup / transcribe / cutscenes / build-iso / xdelta / full
lib/
  afs.py                  # CRI AFS reader + writer
  iso.py                  # ISO9660 patcher (in-place + relocation)
  ship.py                 # canonical hybrid SHIP.AFS builder (D37 architecture)
  cutscenes.py            # demux + re-encode + mux pipeline for SFDs
  ffmpeg.py               # ffmpeg+libass auto-build
  experiments/            # archived diagnostic builds (D24..D37 trail)
docs/ship_afs/            # per-extension SHIP.AFS format reports
roms/                     # place both ISOs here (gitignored)
work/                     # extracted ISO trees (gitignored)
build/                    # output ISO + xdelta (gitignored)
subs/                     # English subtitle files (.ass / .srt) burned into cutscenes
```

## Credit

Inspired by [@soyjxck](https://github.com/soyjxck)'s prior PS2 undub patches:

- [fma-broken-angel-undub](https://github.com/soyjxck/fma-broken-angel-undub)
- [fma-crimson-elixir-undub](https://github.com/soyjxck/fma-crimson-elixir-undub)

SofDec MPEG-PS muxing/demuxing via [`sfd-muxer`](https://github.com/soyjxck/sfd-muxer), ported from [nebulas-star/SFD_Muxer](https://github.com/nebulas-star/SFD_Muxer).
