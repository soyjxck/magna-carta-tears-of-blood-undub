# Translating Magna Carta: Tears of Blood

Short guide for editing the in-game text catalogs. If you're working on the cutscene subtitles, see `subs/korean/*.ass` and `subs/japanese/*.ass` instead — those are separate.

## TL;DR

```bash
python3 patch.py translate-extract        # one-time: dump catalogs to translations/
# … edit `en` fields in translations/<ext>/*.json …
python3 patch.py translate-validate       # check for cap/encoding errors before building
python3 patch.py translate-status         # see how much is done
python3 patch.py build-iso --translations # apply edits and rebuild the ISO
```

## What you can edit

Each catalog entry has reference text in three languages so you can pick whichever source you prefer:

```json
{
  "file": "00016655.cht",
  "ext": ".cht",
  "slots": [
    {
      "en": "What's the Trinity Circle?",
      "kr": "트리니티 서클은 무엇인가요?",
      "jp": "「トリニティサークル」とは何でしょうか？"
    }
  ]
}
```

**Edit only the `en` field.** `kr` and `jp` are reference-only — the build ignores them. Don't add or remove entries from the array; array position is the record/slot/region index. The build re-derives every structural detail (offsets, lengths, caps, headers, trailers) from USA bytes at rebuild time, so the catalog only needs to carry text.

Top-level fields:
- `file` — which SHIP entry this catalog targets. Don't edit.
- `ext` — sanity-check that you're applying the right format. Don't edit.
- `slots` / `regions` / `records` — the array of editable entries.

## Hard rules

- **Latin-1 only.** USA fonts can't render anything outside ISO-8859-1. No smart quotes (`’` → `'`), no em-dashes (`—` → `-`), no ellipsis chars (`…` → `...`), no emoji. The validator will flag the offending character with its position. Accented Latin is fine (`café`, `naïve`).
- **Preserve engine tokens.** See the next section. Dropping these breaks line wrap, voice timing, and UI rendering.
- **Each format has different length rules** (later section).

## Engine tokens

Three things the game treats as runtime instructions, not text. They appear in every region's strings (USA, KR, JP) — leave them exactly as they are.

### `$n` — line break

The literal two-character sequence `$n` is the engine's newline token. Most common token by far (~3,000 occurrences). Dialog boxes wrap a fixed width (~30 chars × 3 lines), so the original text uses `$n` to control line breaks. Dropping one collapses two lines into one that overflows the box.

```
"You look like you're out of breath... $nAre you tired already?"
                                       ↑ line break here
```

When you translate, keep the same `$n` count and put them in roughly the same spots. The validator warns if any go missing.

### `$D<NN>` — engine instruction marker

Two-letter `$D` followed by digits, e.g. `$D04`, `$D06`, `$D08`, `$D20`. Found in **14 records, all in `.fpb`** dialogue. They appear identically in `en` / `kr` / `jp` (giveaway: the engine consumes them at runtime). Likely speaker / portrait / voice-line markers — different value picks a different speaker or portrait.

Records containing `$D<NN>` are typically ATLUS USA's untranslated dialogue (the `en` text is mojibake-from-Shift-JIS), so you'll mostly *see* these in records that look corrupted. **Keep the `$D<NN>` tokens byte-for-byte** when you replace the surrounding text. The validator warns if any go missing.

### `<Specials>`, `<Items Needed>`, `<Monster Info>`, `<Status>` — UI labels

Four `.tui` records (in `00001238.tui` and `00001239.tui`) where the entire `en` value is an angle-bracketed label. The game renders these as styled section headers. You can translate the inner words but **keep the angle brackets**:

```
"<Status>"  →  "<État>"   ✓ ok
"<Status>"  →  "État"     ✗ engine won't render correctly
```

## Format families

Run `python3 patch.py translate-extract` and you'll get JSON catalogs grouped by file extension. The three families behave differently:

### Slot files — hard cap

Extensions: `.cht .odd .gft .cha .cdg .mdg .ecd .fds`

Each slot is one fixed-stride record holding a single null-terminated string plus an optional engine trailer.

```json
{
  "file": "00016655.cht",
  "ext": ".cht",
  "slots": [
    { "en": "What's the Trinity Circle?", "kr": "...", "jp": "..." },
    { "en": "If the enemy's Charisma...", "kr": "...", "jp": "..." }
  ]
}
```

- **Edit only `en`.** Array position equals slot index — the build maps each entry to the same numbered slot in the file.
- **Hard cap per slot**, derived from USA bytes at build time. The validator reports the exact byte budget when an edit overflows: `100 bytes > cap 515`. No need to look up `tail_at` yourself.
- Trailer bytes (some slots have an engine-side metadata block after the string) are preserved verbatim from USA — never visible in the catalog.

### Region-overlay files — hard cap

Extensions: `.pod .tui .itm .abi .sgi .nod .dod .cls .att .val`

Mixed binary/text files — text appears at fixed offsets surrounded by structure (counts, IDs, sentinels) we leave untouched.

```json
{
  "file": "00001240.tui",
  "ext": ".tui",
  "regions": [
    { "en": "Party", "kr": "편성", "jp": "編成" },
    { "en": "Change members participating in battle.", "kr": "...", "jp": "..." }
  ]
}
```

- **Edit only `en`.** Array position equals region index — the build writes each entry's bytes back to the same offset in USA.
- **Hard cap per region**, derived from USA bytes at build time. The build refuses overruns with a precise error.
- Some "regions" are **internal identifiers** the engine reads as fixed strings (asset paths, class names, lookup keys). The `en` text usually tells you which — `Accelerator`, `???_???`, anything CamelCase or with underscores. When in doubt, leave it.
- These files have hard structural sizes (e.g. `.pod` is exactly 12,328 bytes). No soft growth, no file-size drift.

### `.fpb` (PlayBook) — soft cap, per-record

Dialog tables. The catalog has a `records` array; each entry is one dialog line with its own `en` (USA), `kr` (Korean reference), and `jp` (Japanese reference). KR/JP text is sliced from each region's own data section by matching `seq` IDs across regions, so each record's three languages are the same logical line.

```json
{
  "file": "00016751.fpb",
  "records": [
    { "seq": 0, "en": "Are you okay?", "kr": "괜찮으십니까?", "jp": "御怪我はありませんか？" },
    { "seq": 1, "en": "...Yes... Thank you.", "kr": "응...고마워.", "jp": "…うん…ありがとう。" },
    { "seq": 2, "en": "You look like you're out of breath... $nAre you tired already?",
      "kr": "그쯤 싸웠으면, 이제는 슬슬 지칠 때가$n됐겠지?",
      "jp": "あれだけ戦ったら、$nさぞかし疲れているでしょうねぇ？" }
  ]
}
```

- **Edit only `en`.** The build computes `offset` and `length` for each record at rebuild time (running byte counter + length of the encoded `en`). The catalog never stores them, so there's nothing to accidentally desync. `seq` is read-only — it's how the script identifies records ("trigger dialog seq=4"). `kr` and `jp` are reference-only.
- **No hard cap.** Each record's `en` can grow freely; the file's data section is rebuilt by concatenating every record in order.
- **`seq=0` is the implicit prefix record.** Most files use this convention: bytes before the first explicit window form record 0. The catalog materializes it as the first record so every dialog line is editable.
- **Wholesale rewrites are fine.** Each record is independent; rewriting every `en` value works exactly as expected. (Earlier versions used a diff-remap that collapsed under wholesale edits — that's gone.)
- **Records that exist in USA but not KR/JP** (the 21/707 .fpb files with localization-branch deltas) get no `kr` or `jp` field on the unmatched record. You can still translate the `en`.

## Workflow

1. **Extract** — `python3 patch.py translate-extract`
   Reads USA / KR / JP SHIP.AFS, writes 995 catalogs to `translations/<ext>/<basename>.json`. One-time. Repeatable if you want to reset.

2. **Edit** — open the JSONs in any text editor. Change `en` strings. UTF-8 is fine (the build encodes to Latin-1 at write time).

3. **Validate** — `python3 patch.py translate-validate`
   Walks every catalog and flags:
   - Latin-1 violations (with the offending character's position)
   - Cap violations (with byte counts)
   - Edited read-only fields (with the original USA value)
   - Dropped `$n` tokens (warning, not error)
   Exits non-zero on errors. Clean run prints `no issues found.`

4. **Status** — `python3 patch.py translate-status`
   Per-extension table of how many records you've edited so far.

5. **Build** — `python3 patch.py build-iso --translations`
   Applies your edits and produces `build/magna-carta-tears-of-blood-undub-<source>.iso`. Skip the `--translations` flag and the build falls back to raw USA text for everything (vanilla undub). Files without a catalog are passed through untouched, so partial coverage works.

## Common errors

### `non-latin-1 character '…' at position N`
A character in your `en` text isn't representable in Latin-1. Most often: smart quotes pasted from a word processor. Fix:
- `’` `‘` → `'`
- `“` `”` → `"`
- `—` `–` → `-`
- `…` → `...`

### `N bytes > cap M`
Your edit is longer than the slot/region allows. Either:
- Trim the translation, or
- Find a nearby slot you don't need and reuse it (advanced — check the file's role first)

### `slot count mismatch` / `region count mismatch` / `record count mismatch`
You added or removed an entry from the array. The array length must match USA's structure. Re-extract that catalog (delete the file and re-run `translate-extract`).

### `wrong ext: '.foo' != '.cht'`
You changed the top-level `ext` field, or copied a catalog into the wrong directory. Re-extract.

### `$n count dropped: N (USA had M) — line break missing?`
Warning only. Your edited line has fewer `$n` line-break tokens than the original. Sometimes intentional (English is more compact). Sometimes you forgot one. Check by eye.

## What NOT to translate

Some things in the game aren't covered by the JSON catalogs:

- **Cutscene subtitles** — `subs/korean/*.ass` and `subs/japanese/*.ass`. Edit those directly; they're hardsubbed into the video.
- **Menu textures (Party / Item / Equip / Stats tabs, etc.)** — these are pixel-baked into LINEAR.AFS textures. Already overlaid with USA bytes; nothing to translate.
- **Title screen / mode select** — same, baked texture.
- **Source-region voice lines** — those are in MUSIC.AFS as audio. We use the source-region voice deliberately.

If you find a string in-game that isn't in any catalog and isn't in `subs/`, ping me — it might be in one of the file types we haven't decoded yet (`.btv`, `.fld`, `MrtsEngine.u`).

## Reference

For format internals, manifest mechanics, and the rebuild flow, see `TECHNICAL.md` (the engineering doc — much longer, mostly not what a translator needs).
