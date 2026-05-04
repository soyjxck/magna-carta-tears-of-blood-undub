# Technical Notes — Magna Carta: Tears of Blood Undub

The full reverse-engineering record. Documents the engine, every file format we've decoded, the in-place hybrid AFS architecture, the size/manifest discovery that unlocked full English text, the LINEAR.AFS UI-texture overlay that fixed menu/HUD/title-screen rendering, and the per-extension SHIP.AFS taxonomy.

The patch supports two source regions — **Korean** (`SCKA-20043`) and **Japanese** (`SLPM-65947`). Throughout this doc, "**source**" means "whichever of KR or JP is being used as the voice/scene base for that build" (selected with `--source kr|jp`). USA SLUS-21221 is always the structural base.

---

## Table of contents

1. [Engine overview](#engine-overview)
2. [ISO9660 layout](#iso9660-layout)
3. [Boot sequence](#boot-sequence)
4. [CRI AFS format](#cri-afs-format)
5. [The slot-0 manifest — the size source](#the-slot-0-manifest--the-size-source)
6. [SHIP.AFS — game data registry](#shipafs--game-data-registry)
7. [`.fpb` (PlayBook) format](#fpb-playbook-format)
8. [`.pod` (Talisman tutorial popup) format](#pod-talisman-tutorial-popup-format)
9. [`.tui` (UI label) format](#tui-ui-label-format)
10. [Translation guide (editing the JSON catalogs)](#translation-guide-editing-the-json-catalogs)
11. [SHIP.AFS UE2 4-byte stubs](#shipafs-ue2-4-byte-stubs)
12. [LINEAR.AFS — streamed UE2 packages](#linearafs--streamed-ue2-packages)
13. [`.lin` file format](#lin-file-format)
14. [The 31 region-specific `.lin` files](#the-31-region-specific-lin-files)
15. [FILE.AFS — engine packages](#fileafs--engine-packages)
16. [`celfid.lix` — startup bundle](#celfidlix--startup-bundle)
17. [MUSIC.AFS — disjoint voice ID namespaces](#musicafs--disjoint-voice-id-namespaces)
18. [Cutscene SFDs](#cutscene-sfds)
19. [The hybrid undub architecture](#the-hybrid-undub-architecture)
20. [SHIP.AFS overlay policy](#shipafs-overlay-policy)
21. [LINEAR.AFS overlay policy](#linearafs-overlay-policy)
22. [Why we don't disturb the engine](#why-we-dont-disturb-the-engine)
23. [Investigation timeline (the D-build trail)](#investigation-timeline-the-d-build-trail)
24. [Building the patch](#building-the-patch)
25. [Code map](#code-map)
26. [Known limitations](#known-limitations)

---

## Engine overview

**Unreal Engine 2 v118 on PS2**, confirmed by `psx2game.ini` extracted from `FILE.AFS` (and from `celfid.lix`):

```ini
RenderDevice    = PSX2Render.PSX2RenderDevice
AudioDevice     = PSX2Audio.PSX2AudioSubsystem
SofdecDevice    = PSX2Sofdec.PSX2SofdecSubsystem
NetworkDevice   = PSX2NetDrv.PSX2NetDriver
Language        = int                            ; UE2 localization tag (English)
DefaultGame     = MrtsGame.MrtsGameInfo          ; "MRTS" = Magna Carta Real-Time Strategy
```

The boot ELF (`SLUS_212.21` USA, `SCKA_200.43` KR, `SLPM_659.47` JP — all ~4.4 MB) embeds:

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

Plus path templates like `..\FPB\%08d.fpb`, `cdrom0:\`, `Module\cri_adxi.irx`, `Raw\atluslogo.raw`. The `..\<TYPE>\%08d.<ext>` pattern shows how file IDs are formatted: 8-digit zero-padded decimal, with the type as a directory.

---

## ISO9660 layout

USA, KR, and JP share **identical directory structure**. AFS files are loaded by ISO file path (`cdrom0:\MUSIC.afs`), not by hard-coded sectors, so ISO repacking is safe as long as filenames are preserved and the directory record is updated when a file moves. This is what `lib/iso.py` does: it patches new bytes at original LBAs when they fit, and relocates past the original ISO end (with a PVD volume-size grow + ISO9660 directory record update) when they don't.

| File | Purpose | USA | KR | JP |
|---|---|---:|---:|---:|
| `SLUS_212.21` / `SCKA_200.43` / `SLPM_659.47` | Boot ELF | 4,443,312 B | 4,439,344 B | 4,440,016 B |
| `SYSTEM.CNF` | PS2 boot manifest | 57 B | 57 B | 57 B |
| `IOPRP270.IMG` | IOP modules image | 249 KB | 249 KB | 249 KB |
| `MODULE/*.IRX` | IOP drivers (CRI ADX, MC, PAD, SDR, SIO2) | 246 KB | 303 KB | 303 KB |
| `AFS.DIR` | plaintext list `Linear.afs\r\nFile.afs\r\nShip.afs\r\nMUSIC.afs\r\n` | 43 B | 43 B | 43 B |
| `AFSINFO.INI` | preallocated entry-count caps | 33 B | 33 B | 33 B |
| `LINEAR.AFS` | streamed UE2 packages — 4,098 `.lin` + 1 manifest | 370 MB | 370 MB | 370 MB |
| `FILE.AFS` | engine packages, splash bitmaps, configs — 55 entries | 22 MB | 22 MB | 22 MB |
| `SHIP.AFS` | game data registry — 13,921 entries (USA) / 13,862 (KR) / 13,858 (JP) | 46 MB | 46 MB | 46 MB |
| `MUSIC.AFS` | all in-game audio — 5,060 (USA) / 3,646 (KR) / 4,430 (JP) `.adx` files | 1.56 GB | 1.26 GB | 1.28 GB |
| `MOVIE18/*.SFD` | 27 cutscenes (chapter scenes) | 1.0 GB | 970 MB | ~1.0 GB |
| `MOVIE99/*.SFD` | 19 cutscenes (intro/credits/etc.) | 522 MB | 515 MB | ~520 MB |

`AFSINFO.INI` contents:
```
100      ← FILE.AFS entry-count cap   (actual: USA 55)
14000    ← SHIP.AFS entry-count cap   (actual: USA 13921, KR 13862, JP 13858)
5100     ← MUSIC.AFS entry-count cap  (actual: USA 5060)
4100     ← LINEAR.AFS entry-count cap (actual: USA 4099)
750      ← buffer count (sector pool, undocumented)
2000     ← buffer count
```

These are pre-allocation caps the engine reserves at boot. Adding more entries than these caps to any AFS would require updating this file too. We never approach the caps in practice (the hybrid AFS files have the same entry counts as the source region's).

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

Once `?GameMode=Title` is set, the engine loads `00000624.unr` (the title-screen / mode-select Map — see [LINEAR.AFS](#linearafs--streamed-ue2-packages)). That Map orchestrates the rotating camera, the title logo, and the New Game / Load Game / Options menu using textures from other `.lin` files.

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

The 48-byte filename TOC at the end is what `cri_afs.Afs.read_filename_toc()` uses to identify entries by name. The 16-byte trailer is mostly inert — D34 confirmed the engine doesn't validate it.

**Sector alignment**: every entry blob is padded to a 0x800 (2048-byte) sector boundary. `cri_afs.write_afs()` regenerates a valid AFS from a list of `(name, blob)` pairs by recomputing offsets fresh.

The primary TOC's `size` field is just a parser convenience — it is **not** what the engine uses to decide how many bytes to read for a named entry. That comes from the slot-0 manifest, below.

---

## The slot-0 manifest — the size source

**This is the critical discovery that unlocked full English text in SHIP.AFS, and the same trick applies to LINEAR.AFS for the texture overlay.**

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
...
```

For `LINEAR.AFS` the convention is slightly different — entry names are written **without** the `.lin` extension:

```
AFSLINEARFileIndex
0
00000004
45580
00000008
1163178
...
```

Format rules (both archives):
- Line 1: the manifest's own filename / header.
- Line 2: `0` (placeholder for the manifest's own size — the engine reads its actual size from the AFS primary TOC's slot-0 entry).
- Line 3+: alternating `<filename>\r\n<decimal_size>\r\n` for every other entry.

**The engine reads this manifest at AFS load time and uses it as the authoritative source of file sizes for subsequent name-based reads. It does NOT use the AFS primary TOC's u32 size field.**

This means: if you swap a file's bytes in a hybrid AFS but leave the manifest pointing at the original size, the engine reads N bytes from a buffer that's actually M bytes long. For files whose internal layout depends on a length field (`.fpb`, certain UE2 packages), the parser walks past the buffer end → undefined behavior → crash.

The fix: **regenerate the manifest with the actual sizes of bytes you wrote into the hybrid AFS** (`lib/ship.py:build_manifest`, `lib/linear.py:build_manifest`). Done at the same time we rebuild each AFS.

---

## SHIP.AFS — game data registry

USA: 13,921 entries (45.6 MB). KR: 13,862. JP: 13,858. The "game data registry" — every per-scene script, dialog table, lipsync curve, animation reference, item table, talisman effect lookup, tutorial popup, and so on lives here.

### Extension taxonomy (USA)

| Ext | Cnt | Avg size | Role | What's in it |
|---|---:|---:|---|---|
| `.lpt` | 4504 | 286 B | **Lipsync curves** | Mouth-shape curves over time, paired 1:1 with a voice ID by filename |
| `.emi` | 1833 | 1.9 KB | **Emotion/state machines** | Per-character emotion FSM (region-agnostic) |
| `.utx` | 1599 | 4 B | **UE2 texture stubs** | 4-byte handles into LINEAR.AFS / temple.utx — see below |
| `.anm` | 1071 | 4 B | **UE2 animation stubs** | 4-byte handles |
| `.fpb` | 711 | 839 B | **PlayBook dialog tables** | World NPC dialog + in-engine cutscene text |
| `.fld` | 622 | 11.9 KB | **Scene scripts** | Orchestrate cutscenes/dialog/voice triggers/cameras |
| `.cam` | 540 | 257 B | **Camera tracks** | Animation curves for cameras (region-agnostic) |
| `.uax` | 519 | 4 B | **UE2 audio-package stubs** | Region-specific (the only stub kind that genuinely differs in `count`) |
| `.efs` | 515 | 7.6 KB | **Effect scripts** | Particle/effect orchestration |
| `.unr` | 345 | 4 B | **UE2 level stubs** | 4-byte handles |
| `.usx` | 344 | 4 B | **UE2 static-mesh stubs** | 4-byte handles |
| `.ukx` | 341 | 4 B | **UE2 skeletal-mesh stubs** | 4-byte handles |
| `.lkd` | 271 | 144 B | **Looking-direction** | NPC gaze targets |
| `.mes` | 238 | 4 B | **UE2 mesh stubs** | 4-byte handles |
| `.pod` | 148 | 12.3 KB | **Talisman tutorial popups** | Fixed 12,328-byte popup tutorials about Talisman combination effects |
| `.lvt` | 79 | 3.2 KB | **Level voice tables** | Area-keyed ambient/walking voice |
| `.mon` | 48 | 2.1 KB | **Monster data** | Stats/AI hooks |
| `.cht` | 45 | 20.2 KB | **Phone conversations** | Phone dialog + NPC option dialog |
| `.dod` | 40 | 572 B | **Character titles** | Honorifics displayed under names |
| `.ecd` | 25 | 2.1 KB | **Event/cutscene dialog** | Cutscene-tied dialog table |
| `.btv` | 25 | 228 B | **Battle voice triggers** | 54 × u32 voice IDs per file |
| `.tui` | 16 | 14.0 KB | **UI labels** | Menu strings |
| singletons | ~25 | varies | various | `.itm` items, `.cha` characters, `.abi` abilities, `.sgi` styles, `.cls` classes, `.cdg` talisman effects, `.mdg` monster bestiary, `.nod` area names, `.gft` gifts, `.fds` friend dialog, `.odd` quests, plus binary lookups (`.fnd`, `.bsd`, `.bti`, `.qsd`, `.crm`, `.dst`, `.ems`, `.esc`, `.pld`, `.uil`, `.jmu`, `.sop`, `.val`, `.att`, `.seq`, `.pat`) |
| manifest | 1 | 277 KB | `AFSShipFileIndex.idx` slot 0 |

### Cross-references between SHIP entries

```
.fld (scene script)
  ├── triggers .fpb dialog tables          by filename
  ├── plays voice IDs                       → MUSIC.AFS
  ├── schedules .lpt lipsync                (paired 1:1 with voice ID by filename)
  ├── triggers .btv battle voice            54 × u32 voice IDs per .btv file
  ├── sequences .cam camera tracks
  ├── sequences .efs effect scripts
  ├── manipulates .emi state machines
  └── references textures/meshes            via UE2 paths into LINEAR.AFS / temple.utx

.cht (phone/option dialog)                   — text only, indexed by id by .fld
.tui (UI labels)                             — text only, looked up by string ID
.itm/.cdg/.mdg/.pod (data tables)            — text + numeric stats; engine reads by record id
.uax (audio-package stub)                    — 4-byte handle linking SHIP entry to MUSIC voice content
```

---

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

JP: 522 bytes total
  count = 9 (same shape)
  records[0..7]: same seq_ids 1..8, but Japanese text in Shift-JIS
  data_section_size = 406 bytes
```

Records can OVERLAP in the data section (multiple records can read different sub-ranges of the same text pool). Some records read large spans (e.g. record 60 in `00013490.fpb` reads almost the entire 5355-byte data section).

### Cross-region structural equivalence

Across regions, count is almost always identical, only string bytes differ. Of 707 common `.fpb` files, only 21 have a different `count` (those represent localization branch additions/removals — e.g. an extra dialog option in one region). The other 686 are byte-compatible at the record level: same seq_ids, just different per-record `length`/`offset` due to translation length differences.

---

## `.pod` (Talisman tutorial popup) format

`.pod` files are the popup tutorial cards explaining the Talisman combination system — the kind that appear when you first open the Talisman Combine menu. **148 files, every one exactly 12,328 bytes, fixed layout.**

The first 24 bytes are a fixed header (identical across regions):

```
00 00:  1f 00 00 00     u32 record_count       (= 31)
00 04:  01 00 00 00     u32 ?                  (always 1)
00 08:  00 00 00 00     u32 padding
00 0C:  26 05 00 00     u32 data_section_size  (= 1318)
00 10:  70 00 00 00     u32 ?                  (always 0x70 = 112)
00 14:  ...             remaining header bytes are region-specific
```

After the 24-byte header, the rest is a 12,304-byte data section that holds the localized text for the popup. Records are positioned at fixed offsets per `record_count`. Region differences are entirely in the text bytes, not in the header layout.

Example — `00016900.pod`:
- USA: `"The outcome of combining two Talismans is $ndetermined by the relationship between the $ndifferent types of Chi involved..."`
- JP: same offset, Shift-JIS text starting `82cc 9167 82dd 8d87 82ed 82b9 82cd 81a1 ...` ("組み合わせは...")
- KR: same offset, CP949 Korean.

Because the data section is fixed size and records are byte-positioned, USA bytes overlay perfectly into a source-region SHIP — no size change, no manifest disruption needed for `.pod` specifically. (The slot-0 manifest is rebuilt anyway, so it picks up the overlay.)

---

## `.tui` (UI label) format

`.tui` files are flat tables of UI strings: tab labels, menu options, error messages, item categories, system prompts. There are 16 of them in SHIP.AFS. The largest, `00001240.tui` (61,888 bytes), holds the in-game menu (Party / Item / Equip / Style / Status / Settings) and stat labels (Charisma, etc.).

Layout (deduced empirically — full record-format spec is partial):

```
+0x00  u32  count                  (number of label slots)
+0x04  u32  version_or_flags       (= 2 in 00001240)
+0x08  u32  zero                   (padding)
+0x0C  per slot (240 bytes each in 00001240):
         null-terminated label string at the start
         then up to ~228 bytes of zero-padding or trailing description text
```

The label and description live at byte-fixed offsets within a fixed-stride slot, so USA/JP/KR `.tui` files of the same name have the **same total byte size** (61,888 in this case) but different bytes inside each slot. This makes `.tui` overlay byte-budget-safe: USA bytes drop into a source-region SHIP at the same offset.

Example: in `00001240.tui`, the menu-tab labels occupy the first 6 slots:

```
slot 0 @ 0x0c+0:   "Party"     / "編成"
slot 1 @ 0x0c+260: "Item"      / "道具"
slot 2 @ 0x0c+520: "Equip"     / "装備"
slot 3 @ 0x0c+780: "Style"     / "流派"
slot 4 @ 0x0c+1040:"Settings"  / "設定"
slot ~14:          "Status"    / "情報"
```

But — **`.tui` strings are NOT what the engine renders for the menu tabs.** The tabs are rendered from a baked TEXTURE. We confirmed this by overlaying USA `.tui` (so the patched ISO holds "Party"/"Item"/"Equip" at those exact offsets) and watching the rendered tabs stay in the source language. The fix lives in [LINEAR.AFS](#linearafs--streamed-ue2-packages), not SHIP.AFS — the texture pack with the menu-tab pixels is a `.lin` file. The `.tui` string table is used by other UI surfaces (info popups, save-prompt confirmations, possibly mouse-over tooltips), and is overlaid for those.

---

## Translation guide (editing the JSON catalogs)

`patch.py extract-text` (or `python -m lib.translate`) writes per-file JSON catalogs under `translations/<ext>/<basename>.json` for every text-bearing SHIP entry. A translator edits the `en` strings; `build-iso` reads the catalogs back and reassembles the binary file.

There are three format families, and the editing rules differ for each. **Hard rule across all three: only edit the `en` field.** Every other field — `offset`, `length`, `cap`, `tail_at`, `tail_hex`, `i`, `seq`, `header_hex`, `slot_stride`, `size` — is computed from the original USA bytes and used to drive the rebuild. Touching them silently corrupts the output. The `kr` and `jp` fields are reference strings (not used at build time); they're there so the translator can see the source-region wording.

### Common constraints

- **Latin-1 only.** USA fonts on this game can't render anything outside Latin-1 (no smart quotes, em-dashes, ellipsis chars, emoji). `lib/translate/_common.py:encode_en()` rejects non-Latin-1 input at build time so failures are loud, not silent. Use straight ASCII (`'`, `"`, `-`, `...`) and stick to ISO-8859-1 if you need accented Latin characters.
- **Preserve `$n`.** `$n` is the engine's line-break token (visible in `00016900.pod` and many `.fpb` records). It's not whitespace — dropping it collapses multi-line dialog into one overflowing line. Keep the same `$n` count and approximate position the source has.
- **Half-translated repos are fine.** When no catalog exists for a file, the build falls back to the raw USA bytes for that file, so you can ship partial coverage and iterate.

### `.fpb` (windowed-pool) — soft cap, diff-remapped

The catalog is one big editable `en` string holding the whole data section, plus a `windows` array of `{seq, offset, length}` entries. **Don't edit `windows`.** On rebuild, the builder diffs the old vs new `en` via `difflib.SequenceMatcher` and remaps each window's `(offset, length)` so it still bounds the same logical text in the new bytes. This is why dialog records that overlap in the original (one window covering an entire monologue, another covering one line of it) keep working after edits.

- **Soft cap.** Strings can grow freely; the data-section header is rewritten with the new size. The hidden cost is that records whose final `offset + length` would lie outside the new data section get clamped to zero-length — happens in practice only if you delete text that other windows still wanted to point at.
- **Edit semantics.** Changing a word mid-paragraph shifts every window past it; the diff handles that. What can break: rewriting the data section so aggressively that `SequenceMatcher` can't find an "equal" anchor near a window boundary will pin that window to the wrong place. If a particular dialog overshoots after rebuild, keep more of the surrounding source text intact.
- **Keep newlines aligned.** A line of dialog that currently spans 3 game lines will still span 3 game lines after edit only if you keep roughly the same `$n` placement. The dialog box has a fixed character width (~30) and height (~3 lines).

### Slot files (`.cht .odd .gft .cha .cdg .mdg .ecd .fds`) — hard cap

Each catalog has a `slots` array; each slot has `i`, `cap`, `tail_at`, `en`, optionally `tail_hex`, `kr`, `jp`. The hard cap on the English string is **`tail_at - 1` bytes** (one byte reserved for the null terminator).

- `tail_at` = offset where the engine-side trailer starts inside the slot. Equals `cap` when the slot has no trailer; less than `cap` for slots like `.cht`'s 16-byte option-ID block.
- `tail_hex` (if present) is the verbatim trailer block; it is preserved across rebuild and **must not be edited**.
- Exceeding `tail_at - 1` raises `ValueError` at build time with the offending file/slot/length, so over-budget edits don't silently truncate.
- Slot strings are null-padded to `tail_at` on rebuild — you don't need to add nulls in the JSON.

Per-extension stride (informational; the catalog's `slot_stride` mirrors this):

| ext | header | stride | what it is |
|---|---:|---:|---|
| `.cht` | 28 | 532 | phone conversations / NPC option dialog |
| `.odd` | 16 | 240 | side-quest text |
| `.gft` | 16 | 74 | gift dialog |
| `.cha` | 12 | 299 | character bios |
| `.cdg` | 16 | 263 | talisman effect descriptions |
| `.mdg` | 16 | 263 | monster bestiary |
| `.ecd` | 40 | 548 | event/cutscene dialog |
| `.fds` | 12 | 512 | friend/team dialog |

### Region-overlay files (`.pod .tui .itm .abi .sgi .nod .dod .cls .att .val`) — hard cap, in-place

The catalog has a `regions` array; each region has `offset`, `length`, `cap`, `en`, optionally `kr`, `jp`. The hard cap is the `cap` field directly — that's the maximum byte length of the encoded English string.

- `cap` is computed from "how far this text region can grow before hitting the next non-zero structural byte (header field, sentinel, padding, etc.)". The build copies the original USA blob, then overwrites each region with the encoded `en` null-padded to `cap`. Everything else (sentinels, numeric tables, fixed-offset structure) is preserved verbatim.
- Exceeding `cap` raises `ValueError`. There is no soft growth — these files have hard structural constraints (e.g. `.pod` is fixed 12,328 bytes across all 148 files).
- Regions are detected as runs of printable ASCII ≥ 4 chars in the USA blob. Some "text" regions are actually internal identifiers, not user-facing labels — when in doubt, the `en` field's content tells you what's safe to translate vs leave alone.

### Workflow

1. `python -m lib.translate` — extracts every supported extension into `translations/`. Repeatable; safe to re-run.
2. Edit `en` fields in the JSON catalogs. Leave everything else alone.
3. `python patch.py build-iso --source kr|jp` — the builder reads the catalogs and reassembles each file. Files without a catalog fall back to USA bytes. Cap violations stop the build with a precise error.
4. Apply the resulting xdelta and test in PCSX2.

If the build fails with `latin-1 only`, find the offending non-Latin-1 character (smart quotes from a word processor are the usual culprit). If it fails with `string exceeds editable space` or `> cap`, tighten the translation for that record — there's no soft cap on slot/region formats. `.fpb` is forgiving on length but unforgiving on structural rewrites that erase diff anchors.

---

## SHIP.AFS UE2 4-byte stubs

Of SHIP.AFS's 13,921 entries (USA), **4,557 are exactly 4 bytes long** — every `.utx`, `.anm`, `.uax`, `.ukx`, `.unr`, `.usx`, and `.mes` entry is a 4-byte stub. They aren't UE2 packages; the actual packages live in `LINEAR.AFS` as `.lin` files (or in `FILE.AFS` for the global/persistent ones like `temple.utx`). The 4 bytes are size hints / hash IDs the engine uses to size its in-memory texture/anim/sound caches at scene load time.

Only **14 of those 4,557 stubs differ between regions** (USA vs KR vs JP):

| Stub | Class | USA u32 | KR u32 | JP u32 |
|---|---|---:|---:|---:|
| 00000039.unr | level | 792512 | 792512 | 792406 |
| 00000483.unr | level | 2412260 | 2413776 | 2412260 |
| 00000624.unr | level | 10635 | 10738 | 10635 |
| 00002103.unr | level | 1386733 | 1386733 | 1387275 |
| 00007325.unr | level | 898647 | 862542 | 862542 |
| 00016949.unr | level | 949330 | 949330 | 930838 |
| 00000752.utx | texture | 200427 | 200406 | 200406 |
| 00007313.utx | texture | 133756 | 133756 | 133749 |
| 00015559.utx | texture | 396254 | 400480 | 396254 |
| 00001036.ukx | skeletal | 10919457 | 10919522 | 10919457 |
| 00001921.ukx | skeletal | 1818699 | 1818700 | 1818699 |
| 00001076.mes | mesh | 420682 | 420747 | 420682 |
| 00002326.anm | animation | 194973 | 226863 | 194973 |
| 00002670.anm | animation | 191323 | 191323 | 315434 |

These differences track size deltas in the corresponding `.lin` packages in LINEAR.AFS — i.e. `00000752.utx`'s stub differs because USA's `00000752.lin` is 56,166 B while KR's is 61,557 B. We pass through whichever stubs are in the source-region SHIP (KR or JP) base; the corresponding `.lin` in LINEAR.AFS is what we overlay or keep, and that's what actually drives behavior.

Ext counts also differ slightly between regions:
- `.uax` (audio-package stubs): USA 519, KR 519, JP 487. The smaller JP count tracks JP's smaller in-game voice corpus.
- `.lpt` (lipsync): USA 4504, KR 3146, JP 3946. KR/JP have fewer lipsync curves because they have fewer voice IDs.

---

## LINEAR.AFS — streamed UE2 packages

All three regions: 4,099 entries (370 MB). 4,098 `.lin` files + 1 manifest at slot 0.

`.lin` files hold the actual UE2 packages that the engine streams in/out as the player crosses scene boundaries via `FArchiveFileReaderLinear` (string visible in the boot ELF). **Each `.lin` is exactly one UE2 package, of one specific type, identified by the path header at the start of the decompressed buffer.**

### Class breakdown (USA, all 4,098 .lin files)

| Class | Count | Total | Avg | What's inside |
|---|---:|---:|---:|---|
| `.utx` Texture | 1,589 | ~190 MB | ~120 KB | Character portraits, UI graphics, font glyphs, environmental textures |
| `.anm` Animation | 1,070 | ~6 MB | ~6 KB | Keyframe animation data (idle loops, movement curves) |
| `.uax` Audio | 512 | ~50 MB | ~100 KB | Sound effects + voice clips |
| `.efs` EffScr | 281 | ~14 MB | ~50 KB | Effect scripts (combat hits, magic visuals) |
| `.usx` StaticMesh | 255 | ~14 MB | ~55 KB | Environmental objects, signs, props |
| `.mes` Mesh | 209 | ~30 MB | ~145 KB | Character/object 3D meshes (verts, UVs, materials) |
| `.ukx` SkelMesh | 95 | ~20 MB | ~210 KB | Animated character skeletons + bind poses |
| `.unr` Map | 86 | ~25 MB | ~290 KB | Level / scene files |
| `.Emi` Emitter | 1 | ~1 MB | — | Particle effects bundle |

Of the 4,099 entries, **3,993 are byte-identical** between USA and source regions (sample of 200: 200 / 200 identical). Only **31 differ** — and those 31 are where region-specific UI/HUD texture pixels live. See [The 31 region-specific .lin files](#the-31-region-specific-lin-files).

In addition, **74 `.lin` files are USA-only** (no equivalent in KR/JP). These are all `Sounds/.uax` packages — extra English voice content recorded by ATLUS USA that doesn't exist in the source regions. We don't pull these into the hybrid build; they'd reference USA voice IDs that aren't in source-region MUSIC.AFS.

### Mental model: package, not asset

A `.lin` is a *package container*, not a single asset. The package can hold **multiple internal exports of the same type**, and references **other packages' exports** by path.

Example: `00001035.lin` is one `.utx` Texture package containing two texture exports (`c001_calintz_face_index`, `c001_calintz_body_index`) plus the shared UE2 Texture class properties (`USize`, `VSize`, `Palette`, `MipZero`, `UClamp`, `VClamp`, etc.).

Example: `00001076.lin` is one `.mes` Mesh package containing one character mesh — bones (`Bip01`, `Bip01 Neck`, `Bip01 L UpperArm`, `Bip01 Spine1`...), face morph targets (`f_lip_up_midR`, `f_lip_dw_midL`), mesh variants (`eclipse`, `eclipse2`, `eclipse9`). It *references* texture exports from a separate `.utx` `.lin` — those refs show up as `../Textures/...` paths in the import table.

**Why this matters for the hybrid undub**: when the engine loads a character into a scene, it streams in *several* `.lin` files together — Mesh + SkeletalMesh + Animation + Texture — each being one package. Our overlay rules (`USA_OVERLAY_CLASSES = ("Texture", "StaticMesh")`) operate per-package: USA's bytes for all `.utx` and `.usx` packages, source-region bytes for `.mes`/`.ukx`/`.anm`/`.uax`/`.efs`/`.unr`/`.Emi`. Region differences (the 31 differing files) cluster in `.utx` Texture packages because that's where pixel-baked UI text lives.

### Practical implication: where displayed text comes from

Different on-screen text routes to different file types:
- **Static UI labels** (menu tabs "Party"/"Item", title-screen buttons) → pixel-baked into `.utx` Texture packages (auto-overlaid USA)
- **Character names in HUD/dialog** → pixel-baked into character portrait `.utx` packages (auto-overlaid USA — that's why our SHIP `.cha` Khanzada edit didn't change them)
- **Free-form dialog text** → SHIP `.fpb` (catalog-editable)
- **Item/ability descriptions** → SHIP `.itm` / `.abi` regions (catalog-editable)
- **Effect/combat visuals** → `.efs` EffScr packages (one of which can reference a per-character glow texture by name, e.g. `greyglow_for_calintz` in `00011215.efs`)

---

## `.lin` file format

A `.lin` file is a chunked zlib stream that decompresses to one UE2 package. The same format is also used by `celfid.lix` in FILE.AFS.

```
.lin file:
  per chunk:
    u32  uncompressed_size      (always 24576 = 24 KB, except final chunk)
    u32  compressed_size
    bytes[compressed_size]      raw zlib stream (deflate, with zlib header 0x78 0x01 / 0x78 0x9C)

  decompressed (concatenated chunks):
    null-terminated ASCII path header:
      "../Maps/00000624.unr"
      "../Textures/00000752.utx"
      "../StaticMeshes/00011821.usx"
      "../Animations/00015572.anm"      (also "../Anim/...")
      "../Sounds/00026445.uax"
      "..\\EffScr\\00011215.efs"        (effect script — backslash-style path)
      "..\\Emitter\\00003232.Emi"       (particle emitter — backslash-style path)
    + UE2 package starting at a small offset (usually 264 = 0x108) into the decompressed buffer.
```

Decompressed sizes range from ~10 KB (small textures) up to ~570 KB (large levels). Most `.lin` files have 1–6 chunks; very large levels can have 30+.

The first chunk holds the UE2 package header, name table, import table, export table — i.e. the metadata and the path string. Subsequent chunks hold the bulk pixel/geometry/animation data. The path-string header lets us classify each `.lin` by what kind of UE2 asset it carries without having to parse the full UE2 package structure (`lib/linear.py:_classify_lin`).

### UE2 package format (inside the decompressed buffer)

```
+0x00  u32  magic = 0xC1832A9E  (= UE2_MAGIC, little-endian)
+0x04  u16  version              (= 118 for Magna Carta)
+0x06  u16  licensee_version     (12–17 across the assets we've inspected)
+0x08  u32  package_flags
+0x0C  u32  name_count
+0x10  u32  name_offset          (relative to package start)
+0x14  u32  export_count
+0x18  u32  export_offset
+0x1C  u32  import_count
+0x20  u32  import_offset
+0x24  16   guid
+...        Generations TArray, etc.

Name table (each entry):
   compact_int  name_length
   bytes        name_bytes (latin-1)
   u32          name_flags

Import table (each entry):
   compact_int  class_package      (name index)
   compact_int  class_name         (name index)
   i32          package_idx        (object index of containing package)
   compact_int  object_name        (name index)

Export table (each entry):
   compact_int  class_idx          (positive=export, negative=import-1, 0=Class)
   compact_int  super_idx
   i32          package_idx
   compact_int  object_name        (name index)
   u32          object_flags
   compact_int  serial_size
   if serial_size > 0:
     compact_int  serial_offset
```

`compact_int` is UE2's variable-length signed integer — first byte's high bit is sign, second-high bit is "more bytes follow", remaining bits are payload, subsequent bytes use the high bit for continuation.

`lib/experiments/analyze_lin.py` is a reference parser that decompresses a `.lin`, locates the UE2 magic, and dumps the name/import/export tables.

---

## The 31 region-specific `.lin` files

Listed by class. USA / KR / JP sizes show why each entry was flagged (compressed sizes; decompressed sizes are ~3–5× larger).

### Texture (19 files)

These are `.utx` UE2 texture packages — pre-rendered images including menu/HUD/UI text baked into the pixel data. **All are USA-overlaid in the hybrid build.**

| File | UE2 package contents | USA | KR | JP |
|---|---|---:|---:|---:|
| `00000752.lin` | `battle_part1..3` (battle HUD) | 56,166 | 61,557 | 61,866 |
| `00001002.lin` | `field_part1` | 19,307 | 18,620 | 21,656 |
| `00001008.lin` | `global_part1..5` (shared UI) | 53,173 | 54,171 | 54,029 |
| `00005050.lin` | small texture | 15,547 | 16,115 | 16,115 |
| `00005060.lin` | small texture | 12,599 | 11,357 | 11,357 |
| `00008137.lin` | `dojang` (training-room textures) | 6,899 | 11,776 | 11,776 |
| `00013194..00013214.lin` (×5) | small per-stage textures | ~14 KB each | varies | varies |
| `00013627..00013839.lin` (×6) | small per-stage textures | ~17 KB each | varies | varies |
| `00015559.lin` | `title_part1..6` (title screen) | 113,824 | 74,805 | 94,296 |
| `00065533.lin` | `back_part1` (dialog/menu BG) | 891,803 | 891,075 | 886,846 |

### StaticMesh (2 files)

Signboard meshes with text decals baked into the mesh's UV-mapped texture. **USA-overlaid.**

| File | UE2 package contents | USA | KR | JP |
|---|---|---:|---:|---:|
| `00011821.lin` | `signboard01` | 8,247 | 8,247 | 6,949 |
| `00013149.lin` | `signboard02` | 6,408 | 6,408 | 5,473 |

### Map (1 file, USA-overlaid by name)

| File | UE2 package contents | USA | KR | JP |
|---|---|---:|---:|---:|
| `00000624.lin` | `Maps/00000624.unr` — title-screen / mode-select Map | 141,643 | 102,898 | 122,614 |

This is the boot-time Map the engine loads after `?GameMode=Title`. It orchestrates the rotating camera (`MrtsCameraAreas`), the title logo (asset `Title`), and the New Game / Load Game / Options selection. Region-specific because the texture references inside are different per region. We added an explicit `USA_OVERLAY_NAMES = {"00000624.lin"}` allow-list because the class-only policy ("only Texture and StaticMesh") would otherwise keep this Map at the source region. We verified safety: the Map references only 7 assets (5 textures, 1 staticmesh, the level itself), all of which exist in our hybrid; no voice/sound IDs appear in it.

### EffScr (7 files, kept source-region)

Effect scripts that orchestrate particle/emitter sequences. They cross-reference textures, sounds, and other assets by ID; switching to USA could break references. **Kept on source region.**

`00011215.lin`, `00011364.lin`, `00011456.lin`, `00011545.lin`, `00011548.lin`, `00011689.lin`, `00011713.lin`.

### Animation (1 file, kept source-region)

| File | UE2 package contents |
|---|---|
| `00015572.lin` | `Animations/00015572.anm` — animation referencing `MrtsCommand` script hooks |

Skeletal animation with command callbacks. Could carry voice cues; kept source-region for safety.

### Emitter (1 file, kept source-region)

| File | UE2 package contents |
|---|---|
| `00065534.lin` | `Emitter/00003232.Emi` — particle emitter (`side`, `guardglow` textures) |

References textures by ID. Kept source-region.

---

## FILE.AFS — engine packages

USA: 55 entries (22 MB). KR: 53. JP: 56. **Engine + boot data.** Highest-impact members:

| Member | USA | KR | JP | Role |
|---|---:|---:|---:|---|
| `MrtsGame.u` | 5,416,006 | 5,358,748 | 5,337,933 | Game-specific bytecode (battle, dialog, menus). 784 hardcoded English UI strings (USA). |
| `MrtsEngine.u` | 1,628,637 | 2,058,129 | 2,186,823 | Engine-level Mrts classes. KR/JP are larger because they carry double-byte encoding helpers. |
| `Engine.u` | 1,384,472 | 1,384,297 | 1,384,460 | UE2 stock engine. 1129 internal strings. |
| `UWindow.u` | 315,194 | 315,194 | 315,194 | UE2 window/UI system. **Identical across regions.** |
| `Core.u` | 217,733 | 217,733 | 217,733 | UE2 stock core. **Identical across regions.** (Different MD5 because of build-id metadata, but same code.) |
| `Editor.u`, `Gameplay.u`, `UnrealEd.u` | varies | varies | varies | UE2 stock packages. |
| `temple.utx` | 1,006,718 | 1,006,718 | 1,006,718 | Boot textures (logos, fonts). **Byte-identical across all three regions.** |
| `celfid.lix` | 1,153,496 | 1,239,770 | 1,563,043 | zlib-compressed startup bundle (decompresses to `psx2game.ini` + bundled UE2 packages). Region-specific. |
| `Entry.unr` | 7,983 | 7,983 | 7,983 | Boot-time start map. |
| `*.int` (28 files) | varies | varies | varies | UE2 localization manifests. Mostly developer-facing. |
| `*.raw` (12 files) | 860,160 each | varies | varies | Splash bitmaps (atluslogo, banpre, cri, softmax, progressive, Menu_*, PSX2Game). 640×448 raw RGB. Most are byte-identical across regions; `progressive.raw` differs USA=JP vs KR; `atluslogo.raw` is USA-only. |
| `mc.ico` | 52,768 | 52,768 | 52,768 | Memory-card icon. Identical. |

For the undub we keep **all of FILE.AFS USA-side**: the engine code, the splash logos, the fonts in `temple.utx`. KR fonts are designed for Hangul + ASCII; JP fonts are for Shift-JIS + ASCII; USA fonts are ASCII-only. Since we want English text rendered, USA fonts are correct. Any source-region string that survives in the hybrid SHIP renders as `?????` (USA fonts can't represent Hangul/Hiragana/Katakana/Kanji glyph IDs) — which is why we systematically overlay USA text everywhere we can in SHIP.AFS.

---

## `celfid.lix` — startup bundle

`celfid.lix` is one of the most-different files in FILE.AFS — USA: 1.15 MB, KR: 1.24 MB, JP: 1.56 MB compressed. Its on-disk format is identical to a `.lin` file: chunked zlib streams.

Decompressed, it contains:
- `psx2game.ini` (engine config — the file from which UE2 mode is detected).
- `psx2user.ini`.
- A bundled list of UE2 packages, all in the same order across regions: 41 path entries common to all three (`../System/UFile/Engine.u`, etc.), plus a set of bundled texture/UI packages that overlap with LINEAR.AFS texture asset names: `back_part1/3`, `battle_part1..3`, `char_part1..2`, `chat_part1`, `diary_part1`, `field_part1`, `global_part1..5`, `logo_part3`, `map_part1`, `monster_part1`, `result_part1`, `tutorial_part1..2`, `worldmap_part1..5`.

The file is loaded at boot (it's referenced by `psx2game.ini` directives) and prepopulates a slice of UE2 assets so they're available before any LINEAR.AFS streaming. Because we use USA `FILE.AFS`, we get USA `celfid.lix` with English assets. Region differences inside this file account for the bulk of the FILE.AFS size delta between regions, but they don't affect the undub directly because we never stop using USA's copy.

---

## MUSIC.AFS — disjoint voice ID namespaces

| | USA | KR | JP |
|---|---:|---:|---:|
| Total entries | 5,060 | 3,646 | 4,430 |

Only ~498 filenames overlap between any pair, and ~497 of those are byte-identical (= shared BGM, SFX, jingles — region-agnostic audio). Above ID 16,879, USA / KR / JP voice live in completely **disjoint ID ranges**:

- USA-only voice IDs: 4,562 entries / 604 MB (English VO recorded by ATLUS USA) — range 278..29086 in 23 contiguous batches.
- KR-only voice IDs: 3,148 entries / 304 MB (Korean VO recorded by Softmax) — range 278..22562 in 17 contiguous batches.
- JP-only voice IDs: 3,932 entries / ~330 MB — distinct range.

**There is no automatic 1:1 ID mapping.** When ATLUS USA localized the game, they recorded fresh English VO and were assigned new ID blocks; they did NOT reuse KR's IDs. Same story for JP. So to map USA voice ID X to source-region voice ID Y for the "same line," you have to read the `.fld` scene scripts in both regions and infer the pairing — there's no shortcut.

For this undub we sidestep mapping entirely: we use **source-region MUSIC.AFS + source-region scene scripts in SHIP.AFS** so source scripts call source voice IDs that exist in source MUSIC. The USA boot ELF and `MrtsGame.u` don't reference voice IDs directly — they invoke `PlayVoice(id)` generically, with `id` always coming from the data files we control.

---

## Cutscene SFDs

CRI SofDec MPEG-1 Program Stream: 1 video stream `0xE0` (mpeg1video 640×352, ~30 fps, ~5–6 Mbps) + 1 audio stream `0xC0` (ADPCM-ADX 48 kHz stereo, 432 kbps).

**No subtitle stream** — all "subtitle" text is **baked into the video pixels** (USA shows English narration, KR shows Korean Hangul, JP shows Japanese on the same timestamp). Because the burned-in text is in the video plane itself, we cannot losslessly split visuals from language.

SFDs were classified into four tiers vs their source-region counterparts:

| Tier | Definition | Count | Verdict |
|---|---|---:|---|
| 1 | Files are byte-identical | 16 | No-op (silent / logo) |
| 2 | Audio packet count + payload sizes match | 4 | **Audio bytes also match** — no spoken VO to undub |
| 3 | Same duration, different packetization | 1 | Needs custom SofDec muxer |
| 4 | Different cutscene durations | 25 | Cuts diverge between regions; cannot losslessly undub |

For Tier-3/4 cutscenes we use the [`sfd-muxer`](https://pypi.org/project/sfd-muxer/) package (`pip install sfd-muxer`, Python port of nebulas-star/SFD_Muxer) and a re-encode pipeline (`lib/cutscenes.py`):

1. Demux source SFD → MPEG-1 video + ADX audio.
2. Re-encode video at CBR 5500 kbps with English subtitles (pre-generated, shipped in `subs/<source>/`) burned in via libass.
3. Mux back as a fresh SFD using our muxer.

39/46 SFDs successfully re-encoded with source audio + burned English subs. The result: cutscenes show the original source-region character animations + source voice + English subtitles overlaid by us.

### Round-trip notes for `sfd-muxer`

Demuxing an existing SFD and re-muxing it produces **byte-identical
elementary streams** (the video + audio bytes match the input exactly),
but the resulting container can differ from the original in two ways
that don't affect playback:

- **Per-file CRI metadata sector is not preserved.** Original SFDs
  encoded by CRI's reference tooling include a `CRITAGS` private-stream
  PES (sector 3) carrying per-file IDs / version strings / timestamps.
  This sector is editor metadata; the engine ignores it at playback.
  Since the muxer's main use case is taking freshly re-encoded video +
  audio bytes (where there's no original CRITAGS to copy from), it
  doesn't emit a CRITAGS sector. Output is one sector (2048 B) shorter
  than the original would have been.

- **SCR scheduling may drift by ±1 LSB per sector.** The exact System
  Clock Reference value at each pack_head depends on integer-division
  order in the `block_num × sector × 90001 / (mux_rate × 50)` formula;
  small ordering differences vs the C reference don't affect timing or
  sync.

Both differences are inert — the engine reads streams by start codes
and uses each PES's own PTS/DTS for sync, not the pack-head SCR.

---

## The hybrid undub architecture

The full architecture used by `patch.py build-iso`. SOURCE = KR or JP, selected by `--source`.

```
USA boot ELF (SLUS_212.21)         keep            ← engine + ASCII fonts
USA FILE.AFS                       keep            ← MrtsGame.u, splash logos, USA celfid.lix
SOURCE LINEAR.AFS, hybridized      build+swap      ← see "LINEAR.AFS overlay policy"
SOURCE MUSIC.AFS                   swap            ← source voice + region-agnostic BGM/SFX
SOURCE SHIP.AFS, hybridized        build+swap      ← see "SHIP.AFS overlay policy"
re-encoded MOVIE/*.SFD             swap            ← source video + audio + burned EN subs
```

Two new builders are at the heart of the architecture:

- `lib/ship.py` — builds a hybrid SHIP.AFS using SOURCE as the structural base, with USA-byte overlays for text-bearing extensions, and a rebuilt slot-0 manifest.
- `lib/linear.py` — builds a hybrid LINEAR.AFS using SOURCE as the structural base, with USA-byte overlays for Texture/StaticMesh `.lin` files (and one explicitly-allowlisted Map), and a rebuilt slot-0 manifest.

Both builders apply the **same core trick**: rebuild the slot-0 plaintext manifest with the actual sizes of bytes written, so the engine's boot-time size cache matches our hybrid's contents.

Numbers for the canonical JP-source build:

```
SHIP.AFS hybrid:
  total entries:                   13,858
  USA text overlays (17 exts):        288   (.cht .pod .tui .itm .gft .cha .abi
                                              .sgi .cdg .mdg .dod .cls .ecd .att
                                              .nod .val .fds + .fpb blanket)
  kept source-region:              10,582
  manifest: source 232,804 B → rebuilt 214,038 B
  output 40,628,224 B (in-place at USA's 45,647,872 B slot)

LINEAR.AFS hybrid:
  total entries:                    4,025
  USA Texture overlays:                19
  USA StaticMesh overlays:              2
  USA Map overlays (named):             1   (00000624.lin — title screen)
  kept source (differing class):        9   (1 Anim + 7 EffScr + 1 Emitter)
  kept source (identical):          3,993
  source-only entries:                  0
  manifest: source 68,043 B → rebuilt 68,042 B
  output 369,911,808 B (in-place at USA's 370,823,168 B slot)
```

Result on-screen:
- Title screen / New Game / Load Game / Options menu: English (Map + texture overlays).
- In-game menu tabs (Party / Item / Equip / Style / Status / Settings) and stat labels: English (texture overlays).
- Cutscenes: source video + source voice + burned English subs.
- World NPC dialog (`.fpb`): English.
- Phone conversations / NPC option dialog (`.cht`): English.
- Talisman tutorial popups (`.pod`): English.
- UI labels (`.tui`), item names (`.itm`), monster bestiary (`.mdg`), ability descriptions (`.abi`), etc.: English.
- Source-region voice plays under English subtitles/dialog throughout.

---

## SHIP.AFS overlay policy

`lib/ship.py:USA_TEXT_EXTS` lists the extensions that get USA bytes overlaid into the source-region SHIP base:

```python
USA_TEXT_EXTS = (
    ".cht",  # phone conversations + NPC option dialog
    ".pod",  # phone conversation popup tutorials (148 files, 12,328 B fixed)
    ".tui",  # UI labels
    ".itm",  # item names + descriptions
    ".gft",  # gift dialog
    ".cha",  # character data (names, bios)
    ".abi",  # ability definitions
    ".sgi",  # combat-style descriptions
    ".cdg",  # talisman effects
    ".mdg",  # monster bestiary
    ".dod",  # character titles
    ".cls",  # class definitions
    ".ecd",  # event/cutscene dialog
    ".att",  # attribute/stat data
    ".nod",  # area names
    ".val",  # value data
    ".fds",  # friend/team dialog
)
SWAP_FPB = True  # blanket-swap all 711 .fpb (PlayBook) dialog tables
```

These extensions are all **structurally region-equivalent** (same record counts, same lookup scheme by region, only string bytes differ). They're not voice-tied: nothing in `.cht`/`.tui`/`.itm`/etc. references a voice ID. Overlaying USA bytes there is safe regardless of which source region is being used as the base.

`.fpb` is treated as a separate bulk swap because it's the largest text surface (711 files, ~600 KB total) and historically broke the engine before the manifest discovery (D24–D37). With the manifest rebuild in place it's rock-solid.

The manifest rebuild step is non-optional. Without it, the engine reads source-region sizes from the manifest and parses files at the wrong byte length. This is the bug that took D24–D37 to track down — see [Investigation timeline](#investigation-timeline-the-d-build-trail).

---

## LINEAR.AFS overlay policy

`lib/linear.py:USA_OVERLAY_CLASSES` and `USA_OVERLAY_NAMES` together select which `.lin` files get USA bytes:

```python
USA_OVERLAY_CLASSES = ("Texture", "StaticMesh")
USA_OVERLAY_NAMES   = frozenset({
    "00000624.lin",  # Maps/00000624.unr — title-screen / main menu
})
```

Each `.lin` is classified by reading its first chunk's path-string header (`../Textures/`, `../Maps/`, `..\\EffScr\\`, etc. — see [`.lin` file format](#lin-file-format)). The decision flow per entry:

```
for each .lin in SOURCE LINEAR.AFS:
   if not in USA LINEAR or identical to USA → keep source bytes
   else if class in (Texture, StaticMesh)    → use USA bytes
   else if name in USA_OVERLAY_NAMES         → use USA bytes
   else                                       → keep source bytes (region-tied class)
```

The class-based policy comes from one observation: of the 31 region-differing `.lin` files, the only ones that have **no incoming references from voice/scene scripts** are Textures (pure pixel data) and StaticMeshes (geometry + UV). Maps, Animations, Emitters, and EffScrs all reference other assets by ID, and source-region scripts (`.fld` in SHIP) reference them back — switching them to USA could break a region-specific cross-reference.

`00000624.lin` is the named exception: it's a Map (so the class rule rejects it), but **it's the title-screen Map** loaded before any in-game scene. Its references (verified empirically) are limited to texture and staticmesh assets that exist in our hybrid; it has no voice/sound IDs. Allowing its USA overlay flipped the New Game / Load Game / Options menu BG from source-language to English without regressions.

The slot-0 manifest is rebuilt (`build_manifest()` in `linear.py`) just like SHIP. Without that step, the engine reads each `.lin`'s wrong size and the chunked-zlib parser walks into adjacent file bytes.

---

## Why we don't disturb the engine

A "clean" undub instinct is to make the ISO as USA-feeling as possible: USA boot, USA engine, USA assets, just swap voice. We extensively investigated this (D26/D27 — see [Investigation timeline](#investigation-timeline-the-d-build-trail)) and confirmed it's not viable here:

1. **MUSIC.AFS namespaces are disjoint** (above). USA voice IDs don't exist in source MUSIC.AFS and vice versa. To use source voice with USA scripts, you'd need a per-line USA→source ID mapping, and that doesn't exist as a table — it has to be inferred by reading USA's `.fld` and source's `.fld` and pairing them.

2. **`.fld` scene scripts hardcode voice IDs**. USA's `.fld` script for scene N references USA voice IDs. Source's references source voice IDs. The engine doesn't translate.

3. **`.lpt` lipsync curves are timed for specific audio**. Source's `.lpt` matches source voice timing. Putting USA `.lpt` next to source voice produces mouth-flap animations that don't match the audio.

4. **`.btv`/`.lvt` voice trigger tables embed voice IDs as literal u32 values** (verified in `docs/ship_afs/group_A_scene_voice.md`). All 25 USA `.btv` files differ from source's because the IDs differ.

So instead of fighting the voice-trigger graph, we **adopt the source's voice graph wholesale** (source LINEAR + source MUSIC + source scene scripts in SHIP) and overlay only the **text-bearing files** that have no role in voice triggers. Those are mostly self-contained data tables (`.cht`, `.tui`, `.itm`, etc.) plus `.fpb` (once we figured out the manifest) plus the LINEAR Texture/StaticMesh subset (once we figured out where menu pixels live).

---

## Investigation timeline (the D-build trail)

The diagnostic build trail. Each `D<N>` is an ISO we built and tested. Numbering is non-contiguous because the early Ds (D1..D8) experimented with alternative architectures and other dead-ends; D9 is the first stable hybrid; D10..D24 explored font surgery, encoding rewrites, and pure-source builds; D25..D37 explored the .fpb size mystery; D38–D40 are the LINEAR overlay + .pod fix.

| Build | Architecture | Result | Lesson |
|---|---|---|---|
| **D9** | source base SHIP + 16-ext USA text overlay + USA boot + source LINEAR + source MUSIC | works, source voice plays, but `?????` for non-overlaid text | Worked from day one as a baseline. The font/codepage limitation surfaces as `?????` for any source-content file we kept (mostly `.fpb`). |
| **D19/D20** | D9 + UE2 export rewrite to inject source fonts into USA `MrtsEngine.u` | partial fix | Font-table swap helped some glyphs, broke layout (text engine assumed USA glyph metrics). |
| **D22** | Pure source everything | works fully in source language | Confirms source ships a self-consistent graph; just no English text. |
| **D23/D25** | source boot + source FILE.AFS + source everything + USA text overlays | works, source text rendered, English where overlaid | The source-boot route. Worked best for text but used source engine. Also exposed `.fpb` index overruns (D24) which we fixed with structural-safety guards (D25). |
| **D26** | USA boot + USA FILE + source voice-info SHIP overlay | breaks on cutscene trigger | USA's `MrtsGame.u` bytecode isn't compatible with source `.fld` scripts when the voice-info graph is mixed. |
| **D27** | D26 + index-keyed text safety fallback | still breaks | Confirmed the `.fld`-vs-`.fpb` mismatch isn't just count — there's content alignment too. |
| **D28** | D9 + `.pod` + `.odd` + `.fpb` (with safety guard) | breaks before gameplay | Adding `.fpb` to D9's overlay introduced a new failure mode. Initially blamed on `.pod`/`.odd`. |
| **D29** | D9 + just `00001944.fpb` | works | Single-file `.fpb` swap is safe (this file: USA 512 B < KR 530 B). |
| **D30** | D9 + all 707 `.fpb` blanket-swapped | breaks before gameplay | Confirmed the blanket-swap of `.fpb` is the breaker, not `.pod`/`.odd`. |
| **D31** | D9 + count-matched `.fpb` (686 files) | breaks before gameplay | Even with same record count, oversize files break. |
| **D32** | D31 minus `00013490.fpb` | works one more scene, breaks at next | Confirms 00013490 is *one* breaker. Pattern: oversize files. |
| **D33** | D9 + `.fpb` where `USA size <= source size` (161 files) | works fully | Empirical predictor confirmed: `USA <= source` is the safe direction. |
| **D34** | D30 + per-entry trailer metadata from USA | still breaks | The 16-byte AFS trailer isn't what the engine validates. |
| **D35** | D9 + `00013490.fpb` surgically trimmed to source size + patched internal `data_section_size` + record lengths | works | Confirms: trim USA bytes to fit source's byte budget = engine accepts. |
| **D36** | D9 + all 525 oversize `.fpb` trimmed to fit source's byte budget | works, 658/707 in English (93%) | Generic trimmer scales the technique. 49 files have records whose offsets exceed the trim point — those stay source. |
| **D37** | D9 + blanket `.fpb` USA swap + **rebuilt `AFSShipFileIndex.idx` manifest** | works fully, **all 707 `.fpb` in English (100%)** | **The root cause: the engine reads file sizes from the slot-0 plaintext manifest, not the AFS primary TOC.** Rebuilding the manifest with our hybrid's actual sizes makes blanket swap work. No trimming, no lost trailing dialog. |
| **D38** | D37 + JP support + `lib/linear.py` Texture/StaticMesh overlay + rebuilt `AFSLINEARFileIndex.idx` | menu tabs/HUD/title BG English; phone tutorial popups still source | Texture-pack overlay restored English on most baked-in UI text. Revealed `.pod` files as a missed text-bearing extension in SHIP. |
| **D39** | D38 + `.pod` added to `USA_TEXT_EXTS` | menu tabs/HUD English, phone tutorials English, mode-select menu BG still source | `.pod` is the Talisman tutorial popups — fixed-size 12,328 B records that overlay cleanly. Mode-select menu BG remained source-region because the controlling Map (`00000624.lin`) was kept on source by the LINEAR class policy. |
| **D40** | D39 + `USA_OVERLAY_NAMES = {"00000624.lin"}` (title-screen Map) | full English UI from boot through gameplay | Title-screen Map is a Texture/StaticMesh consumer with no voice refs — safe per-name override. **D40 is the canonical build for both KR and JP source.** |

D40 is the canonical architecture, promoted into `lib/ship.py` + `lib/linear.py` and used by `patch.py`.

### The size mystery resolved (D24–D37)

Throughout the D-build trail, the symptom was: when a USA `.fpb` file is BIGGER than its source counterpart, the engine crashes when loading it. The mystery was *why* — the AFS primary TOC has the correct USA size; the per-entry 16-byte metadata trailer is inert; the size doesn't appear hardcoded in `MrtsGame.u`/`MrtsEngine.u`/the boot ELF.

**Answer**: at AFS-load time, the engine reads slot 0 (`AFSShipFileIndex.idx`) — a plaintext `(filename, decimal-size)` table — and uses it as its size cache. Forever after, when something asks "how big is `00013490.fpb`?", the engine answers from this cache. Our hybrid SHIP.AFS was using source's manifest verbatim, which declared the source sizes. So the engine read source's number of bytes from a USA-sized file, got a truncated USA file, and the parser crashed reading past the end of the truncated buffer using the file's larger internal `data_section_size` field.

Why D9's 16-ext text overlays didn't break: most of those files happen to satisfy USA-size <= source-size (English ASCII shorter than CP949 Korean / Shift-JIS Japanese). Where they don't, the file format isn't index-keyed by byte offset, so reading short was harmless.

The fix is `build_manifest()` in `lib/ship.py` and `lib/linear.py`, which writes a fresh `AFS<KIND>FileIndex.idx` with the actual sizes of bytes we wrote.

### The UI-text mystery resolved (D38–D40)

After D37, all in-game `.fpb` dialog and `.cht` phone conversations and the SHIP.AFS text tables were English. But the visible UI elements — menu tabs, title screen, mode-select menu, stat labels — still rendered in the source language. The investigation:

1. **Tested `.tui` first.** Verified `00001240.tui` in the patched ISO had English strings ("Party", "Item", etc.) at the right offsets. The strings were there, but the rendered tabs stayed source-language. Conclusion: the engine isn't using `.tui` strings for tab labels — those are baked TEXTURES.
2. **Tested `temple.utx`.** Byte-identical USA=KR=JP, so it's not the source.
3. **Diffed SHIP.AFS exhaustively.** Found 14 differing 4-byte stubs across regions, but those turned out to be pointers/hashes, not text data. SHIP holds metadata stubs; the actual texture data lives elsewhere.
4. **Decompressed `.lin` files.** Discovered the `.lin` format (chunked zlib + UE2 package). Of 4,025 common `.lin` files, only 31 differ between USA and source. Classified each by UE2 type using its first-chunk path header.
5. **Built D38**: overlaid Texture and StaticMesh `.lin` files. Menu tabs and HUD switched to English.
6. **Phone tutorial popups still source.** Searched USA SHIP for the expected English text "The outcome of combining two Talismans" — found in `00016900.pod`. Added `.pod` to `USA_TEXT_EXTS`. D39 fixes the popups.
7. **Mode-select menu BG still source.** The asset trail led to `00000624.lin` (`Maps/00000624.unr` = title-screen Map). The class policy had skipped it because Maps can carry voice/scene refs, but this specific Map only references textures + staticmeshes. Allowlisted by name in D40.

---

## Building the patch

```
patch.py setup        # extract ISOs, build ffmpeg+libass
patch.py cutscenes    # demux/re-encode/mux all SFDs (using subs/<source>/*.ass) with EN subs burned in
patch.py build-iso    # apply Phase 1 (cutscenes) + Phase 3 (source LINEAR/MUSIC + hybrid SHIP + hybrid LINEAR)
patch.py xdelta       # USA ISO → patched ISO → xdelta3 -e -9 -S djw
patch.py full         # setup + cutscenes + build-iso + xdelta
```

Region selected by `--source kr` or `--source jp`. Defaults to `kr`.

The English subtitle `.ass` files in `subs/korean/` and `subs/japanese/` were transcribed once from the USA SFD audio and committed to the repo. Rebuilds don't re-run transcription.

Apply with: `xdelta3 -d -s 'Magna Carta - Tears of Blood (USA).iso' magna-carta-tears-of-blood-undub-<source>.xdelta out.iso`

---

## Code map

| Module | Role |
|---|---|
| `patch.py` | CLI entry point: `setup` / `cutscenes` / `build-iso` / `xdelta` / `full`. Region selected with `--source kr|jp`. |
| `cri-afs` (PyPI) | CRI AFS reader + writer. Round-trips byte-identically. Used to be `lib/afs.py`; extracted so the FMA repos and other PS2/Dreamcast/GameCube projects can share it. [PyPI](https://pypi.org/project/cri-afs/) · [Repo](https://github.com/soyjxck/cri-afs). |
| `lib/iso.py` | Patches the source USA ISO in place: writes new bytes at original LBAs when they fit, relocates past original ISO end + updates ISO9660 directory entries + grows PVD when they don't. |
| `lib/ship.py` | **Canonical** hybrid SHIP.AFS builder (D37/D39 architecture). Per-extension USA overlay + slot-0 manifest rebuild. |
| `lib/linear.py` | **Canonical** hybrid LINEAR.AFS builder (D38/D40 architecture). Per-class + per-name USA overlay + slot-0 manifest rebuild. |
| `lib/cutscenes.py` | Cutscene pipeline: demux → optional libass hardsub → mux. Drives both the SFD path (Phase-1 of `build-iso`) and the MKV path (`dump-mkv`). |
| `lib/ffmpeg.py` | Auto-builds ffmpeg with libass if not present. |
| `lib/experiments/` | Diagnostic scripts (gitignored). `analyze_lin.py` decompresses `.lin` files and dumps UE2 package structure. |
| `sfd-muxer` (PyPI) | Pure-Python SofDec MPEG-PS muxer/demuxer. Elementary streams round-trip byte-identically; container has minor differences vs original SFDs (see [Round-trip notes](#round-trip-notes-for-sfd-muxer)). [PyPI](https://pypi.org/project/sfd-muxer/) · [Repo](https://github.com/soyjxck/sfd-muxer). |
| `subs/korean/*.ass`, `subs/japanese/*.ass` | Pre-generated English subtitle files for cutscene burn-in (46 each). |

---

## Known limitations

- **Cutscene visuals are source-region's**, not USA's. Tier-3/4 cutscenes have different durations between regions, so we can't put USA video on source audio losslessly. We re-encode source video with English subtitles burned into pixels via libass. No on-screen English narration text from the original USA SFDs.
- **Some `.fpb` files have USA-only content** (4 of them) — these are scenes added in localization that don't exist in source. We skip those in the hybrid; the corresponding scenes don't trigger because the source `.fld` scripts don't reference them.
- **A small amount of text remains source-language** in places we haven't overlaid:
  - Some pixel-baked text on `.lin` files we kept source for safety (Anim, Emitter, EffScr — 9 files for JP source). These are mostly battlefield effects, not user-facing strings.
  - Possibly some hardcoded strings in `MrtsGame.u`/`MrtsEngine.u`. Since we use USA versions of those, English is mostly safe. (`MrtsEngine.u` USA has 334 strings; we haven't found a hardcoded source-language string in the USA bytecode in any test build.)
- **The xdelta is large** (~1.8 GB) because the patch contains region-specific bytes that don't exist anywhere in the source ISO: source voice (~600 MB unique), source LINEAR data (~370 MB different), source scene scripts in SHIP, and 39 re-encoded cutscenes. Lossless undub patches will always be large for games where voice is in a different namespace.
- **Font/codepage**: USA fonts can't render Korean Hangul or Japanese Kana/Kanji glyphs, so any source-language string we leave in the hybrid (for files we can't safely overlay) will display as `?????`. With D40's full text + texture overlay, this affects very little visible content.
