# Magna Carta: Tears of Blood — Korean Undub

Restore the original Korean voice acting in the USA release of *Magna Carta: Tears of Blood* (PS2, SLUS-21221). Built on Unreal Engine 2 with CRI SofDec cutscenes and CRI ADX in-game audio.

> **Status: framework only.** The pipeline (extract → classify → swap → repack → xdelta) works end-to-end, but the simple SFD audio-swap path produces no functional change for this game — see [TECHNICAL.md](TECHNICAL.md). Real undub requires Phase 2 (in-game dialog from `MUSIC.AFS`, mapping voice IDs across regions via UE2 script analysis). WIP.

## Requirements

- Original USA ISO: `Magna Carta - Tears of Blood (USA).iso`
- Original Korean ISO: `Magna Carta - Jinhongui Seongheun (Korea).iso`
- Python 3.11+
- `7z`, `ffmpeg`, `mkisofs` (cdrtools), `xdelta3` on PATH

## Layout

```
patch.py                # CLI — audio / full / build-iso / xdelta
lib/
  afs.py                # CRI AFS archive parser
  sofdec.py             # SofDec MPEG-PS audio-only remuxer (Tier-2 only)
  mpeg_probe.py         # MPEG-PS packet walker (PACK / system / PES)
  sfd_classify.py       # classify each SFD vs its KR counterpart into 4 tiers
  compare_afs.py        # diff two AFS archives by filename + content
  dump_idx.py           # extract entry-0 manifests from each AFS
roms/                   # place both ISOs here (gitignored)
work/                   # extracted ISO trees (gitignored)
build/                  # output ISO + xdelta (gitignored)
subs/                   # reserved for future subtitle work
```

## Pipeline (when there's something to ship)

```bash
python3 patch.py audio        # apply audio-only undub to extracted USA tree
python3 patch.py build-iso    # produce build/magna-carta-tears-of-blood-undub.iso
python3 patch.py xdelta       # produce build/*.xdelta against the original USA ISO
```

`build-iso` works by **copying the original USA ISO and writing patched file bytes at their existing LBAs** — preserves layout exactly, so the resulting xdelta is small.

## Roadmap

- [x] AFS / SofDec format reverse engineering
- [x] SFD tier classification (4 tiers — see TECHNICAL.md)
- [x] Tier-2 audio packet swap (proven mechanically; produces no functional change for any in-game cutscene)
- [ ] **Phase 2 — MUSIC.AFS line pairing** via Unreal `.u` script analysis
- [ ] Phase 3 — Tier-3 SofDec custom muxer (1 cutscene)
- [ ] Phase 4 — Optional lossy mode for Tier-4 cutscenes (whole-SFD swap)

## Credit

Inspired by [@soyjxck](https://github.com/soyjxck)'s prior PS2 undub patches: [`fma-broken-angel-undub`](https://github.com/soyjxck/fma-broken-angel-undub) and [`fma-crimson-elixir-undub`](https://github.com/soyjxck/fma-crimson-elixir-undub).
