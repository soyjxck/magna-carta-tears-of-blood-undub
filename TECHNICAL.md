# Technical Notes

## Engine

Unreal Engine 2 on PS2, confirmed by `psx2game.ini` (extracted from `FILE.AFS`):

```
RenderDevice    = PSX2Render.PSX2RenderDevice
AudioDevice     = PSX2Audio.PSX2AudioSubsystem
SofdecDevice    = PSX2Sofdec.PSX2SofdecSubsystem
NetworkDevice   = PSX2NetDrv.PSX2NetDriver
Language        = int                          ; UE2 localization tag (English)
DefaultGame     = MrtsGame.MrtsGameInfo        ; MRTS = "Magna Carta Real-Time Strategy"
```

Boot ELF embeds the CRI ADX runtime (ADXT/PS2EE Ver.8.94, ADXF/PS2EE Ver.7.12, build Aug 2003).

## ISO layout

USA (SLUS-21221) and KR (SCKA-20043) share identical directory structure. AFS files are loaded **by ISO file path** (`cdrom0:\MUSIC.afs`), not by hard-coded sectors. This means ISO repacking is safe so long as filenames are preserved.

| File | Purpose | USA | KR |
|---|---|---|---|
| `SLUS_212.21` / `SCKA_200.43` | Boot ELF | 4.4 MB | 4.4 MB |
| `SYSTEM.CNF` | PS2 boot manifest | 57 B | 57 B |
| `IOPRP270.IMG` | IOP modules image | 249 KB | 249 KB |
| `MODULE/*.IRX` | IOP drivers (CRI ADX, MC, PAD, SDR, SIO2) | — | — |
| `AFS.DIR` | plaintext AFS filename list (`Linear.afs\r\nFile.afs\r\nShip.afs\r\nMUSIC.afs\r\n`) | 43 B | 43 B |
| `AFSINFO.INI` | plaintext preallocated count caps | 33 B | 33 B |
| `LINEAR.AFS` | scene/level data, 4099 `.lin` files | 354 MB | 353 MB |
| `FILE.AFS` | UE2 packages (Engine.u, MrtsGame.u, …, .ini, .int) | 21 MB | 21 MB |
| `SHIP.AFS` | UE asset registry: 13921 mostly-tiny entries | 44 MB | 44 MB |
| `MUSIC.AFS` | **all in-game audio** (5060 USA / 3646 KR `.adx` files) | 1.46 GB | 1.18 GB |
| `MOVIE18/*.SFD` | 27 cutscenes (chapter scenes) | varies | varies |
| `MOVIE99/*.SFD` | 19 cutscenes (intro/credits/etc.) | varies | varies |

## CRI AFS format

```
0x00  4    magic "AFS\0"
0x04  4    entry_count                 (LE)
0x08  N*8  entries: (offset, size)*    (LE)
...
       8    (toc_offset, toc_size)     (LE) — points at the trailing filename TOC
toc:  per-entry 48 bytes — 32-byte filename (null-padded ASCII) + 16-byte metadata
```

Each AFS has a Magna-Carta-specific manifest at entry 0 (`AFSMUSICFileIndex.idx`, `AFSFileIndex.idx`, etc.) — these turned out to be **plaintext newline-separated filename lists** that mirror the trailing TOC. The engine references audio by filename string.

## MUSIC.AFS — disjoint voice ID namespaces

| | USA | KR | Shared filenames | Shared & byte-equal | Shared & differ |
|---|---:|---:|---:|---:|---:|
| MUSIC | 5060 | 3646 | 498 | 497 | 1 |

Only 498 filenames overlap; 497 of those are byte-identical (= shared BGM/SFX/jingles). USA owns 4562 unique English-VO IDs; KR owns 3148 unique Korean-VO IDs. **There is no automatic 1:1 ID mapping.**

Pairing voice lines requires mining the Unreal `.u` script packages and `.unr` level files for `PlaySound("0000XXXX.adx")` call orderings — same scenes, same call sequence, different IDs.

## SFD cutscenes — the 4-tier classification

CRI SofDec MPEG-1 Program Stream: 1 video stream `0xE0` (mpeg1video 640×352, ~30 fps, ~5–6 Mbps) + 1 audio stream `0xC0` (ADPCM-ADX 48 kHz stereo, 432 kbps). **No subtitle stream** — all "subtitle" text is **baked into the video pixels** (USA shows English narration, KR shows Korean Hangul on the same timestamp).

`lib/sfd_classify.py` puts each USA SFD into one of four tiers vs its KR counterpart:

| Tier | Definition | Count | Verdict |
|---|---|---:|---|
| 1 | Files are byte-identical | 16 | No-op (silent / logo) |
| 2 | Audio packet count + every payload size match | 4 | **Audio bytes also match** — these are music-only narration cutscenes; no spoken VO to undub |
| 3 | Same duration, different packetization | 1 | Needs custom SofDec muxer |
| 4 | Different cutscene durations | 25 | Cuts diverge between regions; cannot losslessly undub |

The kicker: Tier-2 SFDs *look* swap-eligible but their audio is **already byte-identical** between USA and KR — these scenes have no spoken dialog (the narration is the on-screen text). The simple swap pipeline is mechanically correct but functionally a no-op for every cutscene in the game.

Examples:
- `180111`: USA 64.2 s / KR 54.5 s
- `181818`: USA 145.6 s / KR 120.7 s
- `189993`: USA 26.1 s / KR 126.3 s (5×!)

These aren't encoding differences — Korean and US releases were re-cut.

## Why naïve whole-SFD swap is wrong

USA SFD video stream contains **English narration text rendered as pixels** in the MPEG. KR SFD video stream contains **Korean text rendered as pixels** at the same timestamps. Replacing a USA SFD with the KR file would replace English on-screen text with Korean Hangul. A real undub needs USA video + KR audio — but for Tier-3/4 the durations or packet structures don't line up.

## Pipeline mechanics

1. **Extract** both ISOs with `7z x` into `work/usa/` and `work/kr/`.
2. **Classify** each SFD with `lib/sfd_classify.py` (writes JSON report).
3. **Audio swap** (`patch.py audio`): copy USA tree to `build/patched/`, run Tier-2 swap on eligible SFDs, leave everything else untouched.
4. **Build ISO** (`patch.py build-iso`): `cp` original USA ISO, then for each modified file in `build/patched/`, parse the original's LBA from `isoinfo -l` and write the modified bytes at `lba × 2048` in the copy. Files unchanged from USA stay byte-equal — preserves layout perfectly.
5. **xdelta** (`patch.py xdelta`): `xdelta3 -e -9 -S djw` from original USA ISO → patched ISO. Because layout is preserved, the patch is small (KB-range when there are real changes).

## What's next

The real undub work is in `MUSIC.AFS`. The plan:

1. Decompile `MrtsGame.u`, `MrtsEngine.u`, every `.unr` level package (these are stock UE2 packages — `umodel` / `unrealextract` should work).
2. Extract the ordered list of `PlaySound`/`PlayDialog`/equivalent calls per script.
3. Do the same on the Korean release.
4. Pair USA line N ↔ KR line N per script.
5. Rebuild USA `MUSIC.AFS` with KR ADX bytes under the USA filenames the engine expects.

Tier-3 SFD `189992` could use a custom SofDec muxer or fall back to whole-SFD swap if it has no English text overlay.

For Tier-4 cutscenes, the only options are: (a) ship them un-undubbed (English VO retained), or (b) accept whole-SFD swap (Korean audio + Korean text overlay) as a "lossy" mode.
