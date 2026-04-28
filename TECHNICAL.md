# Technical Notes — Magna Carta: Tears of Blood Undub

The full reverse-engineering record. Documents the engine, the file formats, the in-place hybrid AFS architecture, the size/manifest discovery that unlocked full English text, and the per-extension SHIP.AFS taxonomy.

---

## Table of contents

1. [Engine overview](#engine-overview)
2. [ISO9660 layout](#iso9660-layout)
3. [Boot sequence](#boot-sequence)
4. [CRI AFS format](#cri-afs-format)
5. [The slot-0 manifest — the size source](#the-slot-0-manifest)
6. [SHIP.AFS — game data archive](#shipafs)
7. [`.fpb` (PlayBook) format](#fpb-playbook-format)
8. [MUSIC.AFS — disjoint voice ID namespaces](#musicafs)
9. [LINEAR.AFS — level data](#linearafs)
10. [FILE.AFS — engine packages](#fileafs)
11. [Cutscene SFDs](#cutscene-sfds)
12. [The hybrid SHIP architecture (canonical undub)](#the-hybrid-ship-architecture)
13. [Why we don't disturb the engine](#why-we-dont-disturb-the-engine)
14. [Investigation timeline (the D-build trail)](#investigation-timeline)
15. [Known limitations](#known-limitations)

---

## Engine overview

**Unreal Engine 2 on PS2**, confirmed by `psx2game.ini` extracted from `FILE.AFS`:

```ini
RenderDevice    = PSX2Render.PSX2RenderDevice
AudioDevice     = PSX2Audio.PSX2AudioSubsystem
SofdecDevice    = PSX2Sofdec.PSX2SofdecSubsystem
NetworkDevice   = PSX2NetDrv.PSX2NetDriver
Language        = int                            ; UE2 localization tag (English)
DefaultGame     = MrtsGame.MrtsGameInfo          ; "MRTS" = Magna Carta Real-Time Strategy
```

The boot ELF (`SLUS_212.21` USA, `SCKA_200.43` KR, both ~4.4 MB) embeds:

- The CRI ADX runtime (ADXT/PS2EE Ver.8.94, ADXF/PS2EE Ver.7.12, build Aug 2003).
- A SofDec MPEG-PS player (CRI's `PSX2Sofdec.PSX2SofdecSubsystem`).
- The MIPS native code that backs UE2's `MrtsEngine`/`MrtsGame` UnrealScript classes.

Strings inside the boot ELF reveal the load sequence:
```
Initializing CD/DVD
LoadAFS Linear.afs
LoadAFS File.afs
LoadAFS Ship.afs
LoadAFS MUSIC.afs
Starting Engine
?GameMode=Title → Field/Battle/Affect/ViewTexture
```

Plus path templates like `..\FPB\%08d.fpb`, `cdrom0:\`, `Module\cri_adxi.irx`, `Raw\atluslogo.raw`.

---

## ISO9660 layout

USA (SLUS-21221) and KR (SCKA-20043) share **identical directory structure**. AFS files are loaded by ISO file path (`cdrom0:\MUSIC.afs`), not by hard-coded sectors, so ISO repacking is safe as long as filenames are preserved and the directory record is updated when a file moves.

| File | Purpose | USA | KR |
|---|---|---:|---:|
| `SLUS_212.21` / `SCKA_200.43` | Boot ELF | 4,443,312 B | 4,439,344 B |
| `SYSTEM.CNF` | PS2 boot manifest | 57 B | 57 B |
| `IOPRP270.IMG` | IOP modules image | 249 KB | 249 KB |
| `MODULE/*.IRX` | IOP drivers (CRI ADX, MC, PAD, SDR, SIO2) | 246 KB | 303 KB |
| `AFS.DIR` | plaintext list `Linear.afs\r\nFile.afs\r\nShip.afs\r\nMUSIC.afs\r\n` | 43 B | 43 B |
| `AFSINFO.INI` | preallocated entry-count caps (see below) | 33 B | 33 B |
| `LINEAR.AFS` | scene/level UE2 packages — 4,098 `.lin` + 1 manifest | 370 MB | 370 MB |
| `FILE.AFS` | engine packages, splash bitmaps, configs — 55 entries | 22 MB | 22 MB |
| `SHIP.AFS` | game data registry — 13,921 entries (USA) / 13,862 (KR) | 46 MB | 46 MB |
| `MUSIC.AFS` | all in-game audio — 5,060 (USA) / 3,646 (KR) `.adx` files | 1.56 GB | 1.26 GB |
| `MOVIE18/*.SFD` | 27 cutscenes (chapter scenes) | 1.0 GB | 970 MB |
| `MOVIE99/*.SFD` | 19 cutscenes (intro/credits/etc.) | 522 MB | 515 MB |

`AFSINFO.INI` contents:
```
100      ← FILE.AFS entry-count cap   (actual: USA 55)
14000    ← SHIP.AFS entry-count cap   (actual: USA 13921, KR 13862)
5100     ← MUSIC.AFS entry-count cap  (actual: USA 5060)
4100     ← LINEAR.AFS entry-count cap (actual: USA 4099)
750      ← buffer count (sector pool, undocumented)
2000     ← buffer count
```

These are pre-allocation caps the engine reserves at boot. Adding more entries than these caps to any AFS would require updating this file too.

---

## Boot sequence

From the ELF strings + boot logs:

```
1. Reboot IOP                        (PS2's I/O processor)
2. Load IOP modules from Module\:    cri_adxi.irx, libsd.irx, mcman.irx,
                                     mcserv.irx, padman.irx, sdrdrv.irx,
                                     sio2man.irx
3. ADXInit                           (set up ADX audio runtime)
4. Initializing CD/DVD               (mount cdrom0)
5. LoadAFS Linear.afs                (= cdrom0:\LINEAR.AFS)
6. LoadAFS File.afs
7. LoadAFS Ship.afs
8. LoadAFS MUSIC.afs
9. Display splash sequence:          Raw\atluslogo.raw → banpre_logo.raw →
                                     cri_logo.raw → softmax_logo.raw →
                                     progressive.raw
10. Starting Engine
11. Set ?GameMode=Title → Field/Battle/Affect/ViewTexture
```

Each `LoadAFS` calls a routine that:
1. Resolves `cdrom0:\<filename>` → ISO9660 directory entry → starting LBA.
2. Reads the AFS header (magic, entry count, primary entry table).
3. Reads slot 0 — **the manifest** (see next section) — and uses *its* declared sizes to populate the in-memory file-size table.
4. Returns a handle the engine uses for all subsequent name-based lookups.

---

## CRI AFS format

```
+0x00  4    magic = "AFS\0"
+0x04  4    entry_count                       (LE)
+0x08  N*8  entries: u32 offset, u32 size     (LE)        ← primary TOC
+0x...  8   (toc_offset, toc_size)            (LE)        ← back-reference
+0x...       padded to 0x800 (sector boundary)
... per entry: blob padded up to 0x800 ...
+toc_offset:  per entry, 48 bytes:
                 32 B filename (null-padded ASCII)
                 16 B metadata trailer (mostly zero, last 4 B is region-specific)
```

The 48-byte filename TOC at the end is what `lib/afs.py:read_filename_toc()` uses to identify entries by name. The 16-byte trailer is mostly inert — D34 confirmed the engine doesn't validate it.

**Sector alignment**: every entry blob is padded to a 0x800 (2048-byte) sector boundary. `lib/afs.py:write_afs()` regenerates a valid AFS from a list of `(name, blob)` pairs by recomputing offsets fresh.

---

## The slot-0 manifest — the size source

**This is the critical discovery that unlocked full English text.**

Each AFS in the game has a slot-0 entry whose name is `AFS<KIND>FileIndex.idx`:

| Archive | Slot-0 manifest |
|---|---|
| `FILE.AFS` | `AFSFileIndex.idx` |
| `SHIP.AFS` | `AFSShipFileIndex.idx` |
| `LINEAR.AFS` | `AFSLINEARFileIndex.idx` |
| `MUSIC.AFS` | `AFSMUSICFileIndex.idx` |

Each is **plaintext (filename, decimal-size) pairs separated by CRLF**:

```
AFSShipFileIndex.idx
0
00000009.unr
1600498
00000012.unr
3251418
00000018.unr
3399
00000021.unr
3713370
...
```

- Line 1: the manifest's own filename.
- Line 2: `0` (placeholder for the manifest's own size — the engine reads its actual size from the AFS primary TOC's slot-0 entry).
- Line 3+: alternating `<filename>\r\n<decimal_size>\r\n` for every other entry.

**The engine reads this manifest at AFS load time and uses it as the authoritative source of file sizes for subsequent name-based reads. It does NOT use the AFS primary TOC's u32 size field.**

This means: if you swap a file's bytes in a hybrid AFS but leave the manifest pointing at the original size, the engine will read the manifest's old size — short or long — and hand the parser a buffer that doesn't match the file's internal length fields. For `.fpb` files specifically, that mismatch causes the parser to read past the end of allocated memory → crash.

The fix: **regenerate the manifest with the actual sizes of bytes you wrote into the hybrid AFS** (`lib/ship.py:build_manifest`). Done at the same time we rebuild the AFS.

---

## SHIP.AFS

USA: 13,921 entries (45.6 MB). KR: 13,862 entries (45.7 MB). The "game data registry" — every per-scene script, dialog table, lipsync curve, animation reference, item table, and so on lives here.

### Extension taxonomy

Sorted by count (USA SHIP.AFS):

| Ext | USA cnt | Avg size | Role |
|---|---:|---:|---|
| `.lpt` | 4504 | 286 B | **Lipsync curves** — paired 1:1 with a voice ID by filename |
| `.emi` | 1833 | 1.9 KB | Per-character emotion/state machines (region-agnostic) |
| `.utx` | 1599 | 4 B | UE2 texture-package handle (cross-package pointer) |
| `.anm` | 1071 | 4 B | UE2 animation handle |
| `.fpb` | **711** | 839 B | **PlayBook dialog tables** (world NPC dialog, in-engine cutscene text) |
| `.fld` | 622 | 11.9 KB | **Scene scripts** — orchestrate cutscenes/dialog/voice triggers |
| `.cam` | 540 | 257 B | Camera animation tracks (region-agnostic) |
| `.uax` | 519 | 4 B | UE2 audio-package handle (region-specific!) |
| `.efs` | 515 | 7.6 KB | Effect scripts (particles, etc.) |
| `.unr` | 345 | 4 B | UE2 level-package handle |
| `.usx` | 344 | 4 B | UE2 static-mesh handle |
| `.ukx` | 341 | 4 B | UE2 skeletal-mesh handle |
| `.lkd` | 271 | 144 B | Looking-direction (NPC gaze targets) |
| `.mes` | 238 | 4 B | UE2 mesh handle |
| `.pod` | 148 | 12.3 KB | **Talisman descriptions** |
| `.lvt` | 79 | 3.2 KB | **Level voice tables** (area-keyed ambient/walking voice) |
| `.mon` | 48 | 2.1 KB | Monster data |
| `.cht` | 45 | 20.2 KB | **Phone conversations + NPC option dialog** |
| `.dod` | 40 | 572 B | **Character titles** |
| `.ecd` | 25 | 2.1 KB | **Event/cutscene dialog** |
| `.btv` | 25 | 228 B | **Battle voice triggers** — 54 × u32 voice IDs per file |
| `.tui` | 16 | 14.0 KB | **UI labels** |
| singletons | ~25 | varies | `.itm` items, `.cha` characters, `.abi` abilities, `.sgi` styles, `.cls` classes, `.cdg` talisman effects, `.mdg` monster bestiary, `.nod` area names, `.gft` gifts, `.fds` friend dialog, `.odd` quests, plus binary lookups (`.fnd`, `.bsd`, `.bti`, `.qsd`, `.crm`, `.dst`, `.ems`, `.esc`, `.pld`, `.uil`, `.jmu`, `.sop`, `.val`, `.att`, `.seq`, `.pat`) |
| manifest | 1 | 277 KB | `AFSShipFileIndex.idx` (slot 0, plaintext name+size) |

### Cross-references between SHIP entries

```
.fld (scene script)
  ├── triggers .fpb dialog tables    by filename
  ├── plays voice IDs                 → MUSIC.AFS
  ├── schedules .lpt lipsync          (paired 1:1 with voice ID by filename)
  ├── triggers .btv battle voice      54 × u32 voice IDs per .btv file
  ├── sequences .cam camera tracks
  ├── sequences .efs effect scripts
  ├── manipulates .emi state machines
  └── references textures/meshes      via UE2 paths into FILE.AFS / temple.utx

.cht (phone/option dialog)             — text only, indexed by id by .fld
.tui (UI labels)                        — text only, looked up by string ID
.itm/.cdg/.mdg/.pod (data tables)       — text + numeric stats; engine reads by record id
.uax (audio-package handle)             — 4-byte stub linking SHIP entry to MUSIC voice content
```

## `.fpb` (PlayBook) format

`.fpb` files are the PlayBook dialog tables — the engine's `MrtsPlayBookData` class loads them when a scene triggers `Then_Talk_PlayBookLoad("00012345.fpb")`. Each file holds a list of dialog records the script can reference by sequence ID.

### On-disk layout

```
+0x00  u32  count                    (= total slots; last slot is a sentinel, not a record)
+0x04  u32  zero                     (padding)
+0x08  u32  zero                     (padding)
+0x0C  u32  zero                     (padding) — header is 16 bytes total
+0x10  per record (count - 1 of them, 12 bytes each):
         u32 seq_id       (1, 2, 3, ... — sequence number / lookup key)
         u32 length       (bytes of this record's text span)
         u32 offset       (byte offset within data section)
+0x10 + (count-1)*12:
+      u32 data_section_size       (the "sentinel" — total bytes of data section)
+      data section: text strings, indexed by (offset, length) per record
```

Total file size = `16 + (count-1) * 12 + 4 + data_section_size`.

### Reference parser

```python
import struct

def parse_fpb(blob: bytes):
    n = struct.unpack_from("<I", blob, 0)[0]
    real_records = n - 1
    sentinel_off = 16 + real_records * 12
    data_section_size = struct.unpack_from("<I", blob, sentinel_off)[0]
    data_section = blob[sentinel_off + 4:]
    records = []
    for i in range(real_records):
        pos = 16 + i * 12
        seq, length, offset = struct.unpack_from("<III", blob, pos)
        text = data_section[offset:offset + length]
        records.append((seq, text))
    return records
```

### Worked example: `00001944.fpb` (the opening cutscene's dialog)

```
USA: 512 bytes total
  count = 9 (= 8 records + 1 sentinel)
  records[0..7]:
    seq=1, len=54, off=0x0a   "There's a village nearby. \nI'm sure someone..."
    seq=2, len=64, off=0x3d
    ...
  data_section_size = 396 bytes

KR: 530 bytes total
  count = 9 (same shape)
  records[0..7]: same seq_ids 1..8, but Korean text in CP949
  data_section_size = 414 bytes
```

Because records OVERLAP in the data section (multiple records can read different sub-ranges of the same text pool), some records read large spans (e.g. record 60 in `00013490.fpb` reads almost the entire 5355-byte data section).

### Cross-region structural diff

For 00013490.fpb (`Am I nervous about the plan`):
- USA: 6107 B total, count=62 (61 records), data_section_size=5355
- KR: 6091 B total, count=62 (61 records), data_section_size=5339
- Both have seq_ids 1..61 — **logically identical record set**
- Differ only in record `length` and `offset` fields (because USA English strings are different lengths than KR Korean strings)

The 21 of 707 .fpb files where USA and KR have *different* counts represent localization branch additions/removals (e.g. an extra dialog option in one region).

---

## MUSIC.AFS

| | USA | KR | Shared filenames | Shared & byte-equal | Shared & differ |
|---|---:|---:|---:|---:|---:|
| MUSIC | 5060 | 3646 | 498 | 497 | 1 |

Only 498 filenames overlap; **497 of those are byte-identical** (= shared BGM, SFX, jingles — region-agnostic audio). Shared IDs cap at 16,879. Above that, USA and KR voice live in completely disjoint ID ranges:

- USA-only voice IDs: 4,562 entries / 604 MB (English VO recorded by ATLUS USA)
- KR-only voice IDs: 3,148 entries / 304 MB (Korean VO recorded by Softmax)

USA's range: 278..29086, in **23 contiguous batches** (one per recording session).
KR's range: 278..22562, in **17 contiguous batches**.

**There is no automatic 1:1 ID mapping.** When ATLUS USA localized the game, they recorded fresh English VO and were assigned new ID blocks; they did NOT reuse KR's IDs. So to map USA voice ID X to KR voice ID Y for the "same line," you have to read the `.fld` scene scripts in both regions and infer the pairing — there's no shortcut.

For this undub we sidestep mapping entirely: we use **KR MUSIC.AFS + KR scene scripts in SHIP.AFS** so KR scripts call KR voice IDs that exist in KR MUSIC. The USA boot ELF and `MrtsGame.u` don't reference voice IDs directly — they invoke `PlayVoice(id)` generically, with `id` always coming from the data files we control.

---

## LINEAR.AFS

USA: 4,099 entries (370 MB). KR: 4,099 entries (370 MB).

- 4,098 `.lin` files — UE2 packages, one per scene/level. ~89 KB average.
- 1 `AFSLINEARFileIndex.idx` manifest at slot 0.

Streamed in/out as the player crosses level boundaries via `FArchiveFileReaderLinear` (string found in the boot ELF). KR LINEAR.AFS is paired with KR scene scripts in SHIP.AFS — they reference each other by content, so we use KR for both.

---

## FILE.AFS

USA: 55 entries (22 MB). KR: 53 entries (22 MB).

**Engine + boot data.** Highest-impact members:

| Member | USA | KR | Role |
|---|---:|---:|---|
| `MrtsGame.u` | 5,416,006 B | 5,358,748 B | Game-specific bytecode (battle, dialog, menus). 784 hardcoded English UI strings. |
| `MrtsEngine.u` | 1,628,637 B | 2,058,129 B | Engine-level Mrts classes. 334 strings. KR is bigger (?). |
| `Engine.u` | 1,384,472 B | 1,384,297 B | UE2 stock engine. 1129 internal strings. |
| `UWindow.u` | 315,194 B | 315,194 B | UE2 window/UI system. |
| `Core.u`, `Editor.u`, `Gameplay.u`, `UnrealEd.u` | varies | varies | UE2 stock packages. |
| `temple.utx` | 1,006,718 B | 1,006,718 B | Boot textures (logos, fonts). **Identical byte-for-byte between regions.** |
| `celfid.lix` | 1,153,496 B | 1,239,770 B | zlib-compressed embedded mini-filesystem with config (decompresses to `psx2game.ini`+more). Region-specific. |
| `Entry.unr` | 7,983 B | 7,983 B | Boot-time start map. |
| `*.int` (28 files) | varies | varies | UE2 localization manifests. Mostly developer-facing. |
| `*.raw` (12 files) | 860,160 B each | varies | Splash bitmaps (atluslogo, banpre, cri, softmax, progressive, Menu_*, PSX2Game). 640×448 raw. |

For the undub we keep **all of FILE.AFS USA-side**: the engine code, the splash logos, the fonts in `temple.utx`. KR fonts are designed for Hangul + ASCII; USA fonts are ASCII-only. Since we want English text rendered, USA fonts are correct. (USA's fonts can't render CP949, so any Korean string in our hybrid SHIP renders as `?????` — which is why we overlay USA text everywhere we can.)

---

## Cutscene SFDs

CRI SofDec MPEG-1 Program Stream: 1 video stream `0xE0` (mpeg1video 640×352, ~30 fps, ~5–6 Mbps) + 1 audio stream `0xC0` (ADPCM-ADX 48 kHz stereo, 432 kbps).

**No subtitle stream** — all "subtitle" text is **baked into the video pixels** (USA shows English narration, KR shows Korean Hangul on the same timestamp).

SFDs were classified into four tiers vs their KR counterparts:

| Tier | Definition | Count | Verdict |
|---|---|---:|---|
| 1 | Files are byte-identical | 16 | No-op (silent / logo) |
| 2 | Audio packet count + payload sizes match | 4 | **Audio bytes also match** — no spoken VO to undub |
| 3 | Same duration, different packetization | 1 | Needs custom SofDec muxer |
| 4 | Different cutscene durations | 25 | Cuts diverge between regions; cannot losslessly undub |

For Tier-4 cutscenes we use the [`sfd-muxer`](https://github.com/soyjxck/sfd-muxer) package (Python port of nebulas-star/SFD_Muxer, byte-identical to the C reference) and a re-encode pipeline (`lib/cutscenes.py`):

1. Demux KR SFD → MPEG-1 video + ADX audio.
2. Re-encode video at CBR 5500 kbps with English subtitles (pre-generated, shipped in `subs/`) burned in via libass.
3. Mux back as a fresh SFD using our muxer.

39/46 SFDs successfully re-encoded with KR audio + burned English subs. The result: cutscenes show the original Korean character animations + Korean voice + English subtitles overlaid by us.

---

## The hybrid SHIP architecture (canonical undub)

The full architecture used by `patch.py build-iso`:

```
USA boot ELF (SLUS_212.21)         keep   ← engine + ASCII fonts
USA FILE.AFS                       keep   ← MrtsGame.u, splash logos
KR  LINEAR.AFS                     swap   ← level data paired with KR scene scripts
KR  MUSIC.AFS                      swap   ← Korean voice + region-agnostic BGM/SFX
hybrid SHIP.AFS                    swap   ← see below
re-encoded MOVIE/*.SFD             swap   ← KR video + Korean audio + burned EN subs
```

The hybrid SHIP.AFS is built by `lib/ship.py`:

```
1. Iterate KR's 13,862 entries (KR base = scene/voice graph stays internally consistent).
2. For each entry:
     If extension in {.cht, .tui, .itm, .gft, .cha, .abi, .sgi, .cdg, .mdg,
                      .dod, .cls, .ecd, .att, .nod, .val, .fds}
        AND filename exists in USA SHIP:
        → use USA bytes (English text)
     Elif extension == .fpb AND filename exists in USA SHIP:
        → use USA bytes (English world dialog — full content)
     Else:
        → use KR bytes (voice scripts, lipsync, structural data)
3. Rebuild slot-0 manifest (AFSShipFileIndex.idx) with the actual byte sizes
   of the entries we just assembled. THIS IS THE KEY STEP.
4. Pass through KR's 16-byte trailing TOC metadata block (engine ignores it).
5. write_afs() emits the new SHIP.AFS with sector-aligned blobs and a fresh
   primary TOC.
```

Numbers for the canonical build:

```
total entries:                    13,862
USA text overlays (16 exts):         140
.fpb blanket-swapped to USA:         707
kept KR (voice/scene/structure):  13,015
manifest: KR original 275,683 B → rebuilt 257,064 B
SHIP.AFS hybrid: ~45.5 MB (fits within USA's 45.6 MB ISO slot — in-place)
```

Result on-screen:
- Cutscenes: KR video + Korean voice + burned English subs.
- World NPC dialog (`.fpb`): English.
- Phone conversations / NPC option dialog (`.cht`): English.
- UI labels (`.tui`), item names (`.itm`), monster bestiary (`.mdg`), ability descriptions (`.abi`), etc.: English.
- Korean voice plays under English subtitles/dialog throughout.

---

## Why we don't disturb the engine

A "clean" undub instinct is to make the ISO as USA-feeling as possible: USA boot, USA engine, USA assets, just swap voice. We extensively investigated this (D26/D27 — see [Investigation timeline](#investigation-timeline)) and confirmed it's not viable here:

1. **MUSIC.AFS namespaces are disjoint** (above). USA voice IDs don't exist in KR MUSIC.AFS and vice versa. To use KR voice with USA scripts, you'd need a per-line USA→KR ID mapping, and that doesn't exist as a table — it has to be inferred by reading USA's `.fld` and KR's `.fld` and pairing them.

2. **`.fld` scene scripts hardcode voice IDs**. USA's `.fld` script for scene N references USA voice IDs. KR's references KR voice IDs. The engine doesn't translate.

3. **`.lpt` lipsync curves are timed for specific audio**. KR's `.lpt` matches KR voice timing. Putting USA `.lpt` next to KR voice produces mouth-flap animations that don't match the audio.

4. **`.btv`/`.lvt` voice trigger tables embed voice IDs as literal u32 values** (verified in `docs/ship_afs/group_A_scene_voice.md`). All 25 USA `.btv` files differ from KR's because the IDs differ.

So instead of fighting the voice-trigger graph, we **adopt KR's voice graph wholesale** (KR LINEAR + KR MUSIC + KR scene scripts in SHIP) and overlay only the **text-bearing files** that have no role in voice triggers — those are mostly self-contained data tables (`.cht`, `.tui`, `.itm`, etc.) plus `.fpb` once we figured out the manifest.

---

## Investigation timeline

The diagnostic build trail. Each `D<N>` is an ISO we built and tested. Numbering is non-contiguous because the early Ds (D1..D8) experimented with alternative architectures and other dead-ends; D9 is the first stable hybrid; D10..D24 explored font surgery, encoding rewrites, and pure-KR builds; D25..D37 explored the .fpb size mystery.

| Build | Architecture | Result | Lesson |
|---|---|---|---|
| **D9** | KR base SHIP + 16-ext USA text overlay + USA boot + KR LINEAR + KR MUSIC | works, KR voice plays, but `?????` for non-overlaid text | Worked from day one as a baseline. The font/codepage limitation surfaces as `?????` for any KR-content file we kept (mostly `.fpb`). |
| **D19/D20** | D9 + UE2 export rewrite to inject KR fonts into USA `MrtsEngine.u` | partial fix | Font-table swap helped some glyphs, broke layout (text engine assumed USA glyph metrics). |
| **D22** | Pure KR everything | works fully in KR | Confirms KR ships a self-consistent graph; just no English text. |
| **D23/D25** | KR boot + KR FILE.AFS + KR everything + USA text overlays | works, KR text rendered, English where overlaid | The KR-boot route. Worked best for text but used KR engine. Also exposed `.fpb` index overruns (D24) which we fixed with structural-safety guards (D25). |
| **D26** | USA boot + USA FILE + KR voice-info SHIP overlay | breaks on cutscene trigger | USA's `MrtsGame.u` bytecode isn't compatible with KR `.fld` scripts when the voice-info graph is mixed. |
| **D27** | D26 + index-keyed text safety fallback | still breaks | Confirmed the `.fld`-vs-`.fpb` mismatch isn't just count — there's content alignment too. |
| **D28** | D9 + `.pod` + `.odd` + `.fpb` (with safety guard) | breaks before gameplay | Adding `.fpb` to D9's overlay introduced a new failure mode. Initially blamed on `.pod`/`.odd`. |
| **D29** | D9 + just `00001944.fpb` | works | Single-file `.fpb` swap is safe (this file: USA 512 B < KR 530 B). |
| **D30** | D9 + all 707 `.fpb` blanket-swapped | breaks before gameplay | Confirmed the blanket-swap of `.fpb` is the breaker, not `.pod`/`.odd`. |
| **D31** | D9 + count-matched `.fpb` (686 files) | breaks before gameplay | Even with same record count, oversize files break. |
| **D32** | D31 minus `00013490.fpb` | works one more scene, breaks at next | Confirms 00013490 is *one* breaker. Pattern: oversize files. |
| **D33** | D9 + `.fpb` where `USA size <= KR size` (161 files) | works fully | Empirical predictor confirmed: `USA <= KR` is the safe direction. |
| **D34** | D30 + per-entry trailer metadata from USA | still breaks | The 16-byte AFS trailer isn't what the engine validates. |
| **D35** | D9 + `00013490.fpb` surgically trimmed to KR size + patched internal `data_section_size` + record lengths | works | Confirms: trim USA bytes to fit KR's byte budget = engine accepts. |
| **D36** | D9 + all 525 oversize `.fpb` trimmed to fit KR's byte budget | works, 658/707 in English (93%) | Generic trimmer scales the technique. 49 files have records whose offsets exceed the trim point — those stay KR. |
| **D37** | D9 + blanket `.fpb` USA swap + **rebuilt `AFSShipFileIndex.idx` manifest** | works fully, **all 707 `.fpb` in English (100%)** | **The root cause: the engine reads file sizes from the slot-0 plaintext manifest, not the AFS primary TOC.** Rebuilding the manifest with our hybrid's actual sizes makes blanket swap work. No trimming, no lost trailing dialog. |

D37 is the canonical architecture, promoted into `lib/ship.py` and used by `patch.py`.

### The size mystery resolved

Throughout the D-build trail, the symptom was: when a USA `.fpb` file is BIGGER than its KR counterpart, the engine crashes when loading it. The mystery was *why* — the AFS primary TOC has the correct USA size; the per-entry 16-byte metadata trailer is inert; the size doesn't appear hardcoded in `MrtsGame.u`/`MrtsEngine.u`/the boot ELF.

**Answer**: at AFS-load time, the engine reads slot 0 (`AFSShipFileIndex.idx`) — a plaintext `(filename, decimal-size)` table — and uses it as its size cache. Forever after, when something asks "how big is `00013490.fpb`?", the engine answers from this cache. Our hybrid SHIP.AFS was using KR's manifest verbatim, which declared the KR sizes. So the engine read KR's number of bytes from a USA-sized file, got a truncated USA file, and the parser crashed reading past the end of the truncated buffer using the file's larger internal `data_section_size` field.

Why D9's D9-ext text overlays didn't break: most of those files happen to satisfy USA-size <= KR-size (English ASCII shorter than CP949 Korean). Where they don't, the file format isn't index-keyed by byte offset, so reading short was harmless.

The fix is `build_manifest()` in `lib/ship.py`, which writes a fresh `AFSShipFileIndex.idx` with the actual sizes of bytes we wrote.

---

## Known limitations

- **Cutscene visuals are KR's**, not USA's. Tier-4 cutscenes have different durations between regions, so we can't put USA video on KR audio losslessly. We re-encode KR video with English subtitles burned into pixels via libass. No on-screen English narration text from the original USA SFDs.
- **Some `.fpb` files have USA-only content** (4 of them) — these are scenes added in localization that don't exist in KR. We skip those in the hybrid; the corresponding scenes don't trigger because the KR `.fld` scripts don't reference them.
- **Some text remains Korean** in places we haven't overlaid: occasional menu strings stored inside `.lin` (level data) or hardcoded in `MrtsGame.u`. The latter is USA's bytecode so most engine-level text is already English.
- **The xdelta is large** (~1.8 GB) because the patch contains region-specific bytes that don't exist anywhere in the source ISO: KR voice (~600 MB unique), KR LINEAR data (~370 MB different), KR scene scripts in SHIP, and 39 re-encoded cutscenes. Lossless undub patches will always be large for games where voice is in a different namespace.
- **Font/codepage**: USA fonts can't render Korean glyphs, so any Korean string we leave in the hybrid (for files we can't safely overlay) will display as `?????`. With D37's full `.fpb` overlay, this affects very little visible text.

---

## Building the patch

```
patch.py setup        # extract ISOs, build ffmpeg+libass
patch.py cutscenes    # demux/re-encode/mux all SFDs (using subs/*.ass) with EN subs burned in
patch.py build-iso    # apply Phase 1 (cutscenes) + Phase 3 (KR LINEAR/MUSIC + hybrid SHIP)
patch.py xdelta       # USA ISO → patched ISO → xdelta3 -e -9 -S djw
patch.py full         # setup + cutscenes + build-iso + xdelta
```

The English subtitle `.ass` files in `subs/` were transcribed once from the USA SFD audio and committed to the repo. Rebuilds don't re-run transcription.

Apply with: `xdelta3 -d -s 'Magna Carta - Tears of Blood (USA).iso' magna-carta-tears-of-blood-undub.xdelta out.iso`

---

## Code map

| Module | Role |
|---|---|
| `patch.py` | CLI entry point: `setup` / `cutscenes` / `build-iso` / `xdelta` / `full`. |
| `lib/afs.py` | CRI AFS reader + writer. Round-trips byte-identically. |
| `lib/iso.py` | Patches the source USA ISO in place: writes new bytes at original LBAs when they fit, relocates past original ISO end + updates ISO9660 directory entries + grows PVD when they don't. |
| `lib/ship.py` | **Canonical** hybrid SHIP.AFS builder (D37 architecture). Builds the slot-0 manifest. |
| `lib/cutscenes.py` | Phase-1 cutscene re-encode pipeline (demux → re-encode video with libass → mux). |
| `lib/ffmpeg.py` | Auto-builds ffmpeg with libass if not present. |
| `sfd-muxer` (external) | Pure-Python SofDec MPEG-PS muxer/demuxer (byte-identical to reference C). [Own repo](https://github.com/soyjxck/sfd-muxer). |
| `subs/*.ass` | Pre-generated English subtitle files for cutscene burn-in. |
