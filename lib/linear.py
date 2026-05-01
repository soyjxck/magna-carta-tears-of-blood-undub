"""Hybrid LINEAR.AFS builder for the Magna Carta undub.

Architecture
------------
LINEAR.AFS holds the streamed UE2 asset packages: per-level Textures, Maps,
StaticMeshes, Animations, EffScr (effect scripts), and Emitter particle
effects, each compressed as a `.lin` (zlib chunks of the underlying
`.utx` / `.unr` / `.usx` / `.anm` / `.efs` / `.Emi`).

Of the ~4,000 .lin files, only ~31 differ between USA and the source
region. Most of those differences are **region-specific UI/HUD textures
with menu labels baked in** — they're why menu tabs (Party/Item/Equip),
the Charisma stat label, etc., still render in JP/KR after we swap
LINEAR.AFS wholesale. Overlaying USA bytes for the texture+signboard
subset of those .lin files restores English UI without disturbing
voice/scene paths.

Strategy:
  - Source region (KR or JP) LINEAR.AFS as the structural base.
  - USA-overlay only the .lin files whose UE2 type is `Texture` or
    `StaticMesh` (the signboards in MagnaCarta carry baked-in text).
  - Keep source-region for `EffScr`, `Map`, `Anim`, `Emitter` — those
    cross-reference voice/scene IDs by index and would break with a USA
    overlay.
  - Rebuild slot-0 manifest (`AFSLINEARFileIndex.idx`) with the actual
    sizes of bytes we wrote — same trick as SHIP.AFS slot 0.

The slot-0 manifest rebuild is critical: the engine uses this plaintext
table (not the AFS primary TOC) as the authoritative size source. Without
it, USA-bigger overlays read short and the parser walks past the buffer.
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from afs import Afs, write_afs


MANIFEST_NAME = "AFSLINEARFileIndex.idx"
MANIFEST_HEADER = "AFSLINEARFileIndex"  # written without the .idx in the body


def _first_chunk_path(blob: bytes) -> str:
    """Return the leading "../<Type>/<id>.<ext>" header from a .lin file's
    first zlib chunk (or empty string if the file isn't a valid chunked
    UE2 package)."""
    if len(blob) < 8:
        return ""
    _unc, cmp = struct.unpack_from("<II", blob, 0)
    if cmp <= 0 or 8 + cmp > len(blob):
        return ""
    try:
        c = zlib.decompress(blob[8 : 8 + cmp])
    except zlib.error:
        return ""
    end = c.find(b"\x00", 0, 96)
    if end < 0:
        return ""
    return c[:end].decode("ascii", errors="replace")


def _classify_lin(blob: bytes) -> str:
    """Return UE2 asset class: 'Texture' / 'StaticMesh' / 'Map' / 'Anim' /
    'EffScr' / 'Emitter' / 'Other'. Distinguishes the safe-to-overlay
    visual assets (Texture, StaticMesh) from the cross-referencing ones."""
    p = _first_chunk_path(blob)
    pl = p.lower()
    if "/textures/" in pl or "\\textures\\" in pl:
        return "Texture"
    if "/staticmeshes/" in pl or "\\staticmeshes\\" in pl:
        return "StaticMesh"
    if "/maps/" in pl or "\\maps\\" in pl:
        return "Map"
    if "/anim/" in pl or "\\anim\\" in pl:
        return "Anim"
    if "/effscr/" in pl or "\\effscr\\" in pl:
        return "EffScr"
    if "/emitter/" in pl or "\\emitter\\" in pl:
        return "Emitter"
    return "Other"


# Asset classes we overlay with USA bytes. Textures and StaticMesh
# signboards bake in language-specific text; everything else can carry
# region-tied ID references and is left on the source region.
USA_OVERLAY_CLASSES = ("Texture", "StaticMesh")

# Specific .lin files that need USA bytes regardless of class. The
# title-screen / mode-select Map (00000624.unr) orchestrates the
# "New Game / Load Game" scene with region-baked UI text — keeping JP
# leaves the menu BG in the source language. Verified: only references
# Texture / StaticMesh assets, no voice/sound IDs, so the USA Map is
# safe to plug in alongside the JP MUSIC/voice base.
USA_OVERLAY_NAMES = frozenset({
    "00000624.lin",  # Maps/00000624.unr — title-screen / main menu
    # ── batch HUD-investigation flip (top 4 source-kept candidates) ──
    "00065534.lin",  # Emitter side + guardglow + 26 textures
    "00011548.lin",  # EffScr  n_glow1 + 13 textures
    "00011713.lin",  # EffScr  rock2 + 'cur' keyword + 9 textures
    "00011215.lin",  # EffScr  kongpa (Korean sparring term)

})


def build_manifest(entries: list[tuple[str, bytes]]) -> bytes:
    """Build the LINEAR.AFS slot-0 manifest from entries' actual sizes.

    Format (plaintext, CRLF-delimited, ASCII):

      AFSLINEARFileIndex\\r\\n
      0\\r\\n                      <- self-size placeholder
      00000004\\r\\n                <- entry name, '.lin' stripped
      45580\\r\\n                   <- entry size in decimal
      00000008\\r\\n
      1163178\\r\\n
      ...
    """
    lines = [MANIFEST_HEADER, "0"]
    for name, blob in entries:
        if name == MANIFEST_NAME:
            continue
        # Strip .lin extension to match the original manifest convention
        stem = name[:-4] if name.lower().endswith(".lin") else name
        lines.append(stem)
        lines.append(str(len(blob)))
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def build(out_path: Path | None = None,
          usa_linear: Path = ROOT / "work" / "usa" / "LINEAR.AFS",
          src_linear: Path | None = None,
          source: str = "kr",
          verbose: bool = True) -> Path:
    """Build a hybrid LINEAR.AFS using `src_linear` (KR or JP) as the
    structural base, with USA bytes overlaid for Texture and StaticMesh
    .lin files that carry baked-in UI text."""
    if src_linear is None:
        src_linear = ROOT / "work" / source / "LINEAR.AFS"
    if out_path is None:
        out_path = ROOT / "build" / f"{source}_base" / "LINEAR.AFS"

    out_path.parent.mkdir(parents=True, exist_ok=True)

    usa = Afs.open(usa_linear); usa_n = usa.read_filename_toc()
    src = Afs.open(src_linear); src_n = src.read_filename_toc()
    usa_idx = {n.lower(): i for i, n in enumerate(usa_n)}

    entries: list[tuple[str, bytes]] = []
    swapped = {cls: 0 for cls in USA_OVERLAY_CLASSES}
    kept_src_diff = 0   # files that differ between regions but we keep source
    kept_src_same = 0   # files identical between regions
    not_in_usa = 0      # source-only files

    with src_linear.open("rb") as fh_src, usa_linear.open("rb") as fh_usa:
        for i, name in enumerate(src_n):
            ln = name.lower()
            if not ln.endswith(".lin") or ln not in usa_idx:
                # manifest, source-only files, or non-.lin entries → keep source
                if ln not in usa_idx and i != 0:
                    not_in_usa += 1
                entries.append((name, src.read_entry(i, fh_src)))
                continue

            src_blob = src.read_entry(i, fh_src)
            usa_blob = usa.read_entry(usa_idx[ln], fh_usa)

            if src_blob == usa_blob:
                # identical between regions — picking either is fine; keep source
                entries.append((name, src_blob))
                kept_src_same += 1
                continue

            # Differs. Classify and decide.
            cls = _classify_lin(usa_blob)
            if cls in USA_OVERLAY_CLASSES or ln in USA_OVERLAY_NAMES:
                entries.append((name, usa_blob))
                key = cls if cls in USA_OVERLAY_CLASSES else f"{cls} (named)"
                swapped[key] = swapped.get(key, 0) + 1
            else:
                entries.append((name, src_blob))
                kept_src_diff += 1

    # Rebuild slot-0 manifest with the actual sizes we wrote.
    assert entries[0][0] == MANIFEST_NAME, (
        f"slot 0 should be {MANIFEST_NAME}, got {entries[0][0]}"
    )
    new_manifest = build_manifest(entries)
    if verbose:
        print(f"  manifest: source-region original {src.entries[0].size:,} B → "
              f"rebuilt {len(new_manifest):,} B")
    entries[0] = (MANIFEST_NAME, new_manifest)

    src_meta = src.read_toc_metadata()

    if verbose:
        print(f"  source: {src_linear}")
        print(f"  total entries: {len(entries)}")
        for cls, n in swapped.items():
            print(f"  USA overlay [{cls}]: {n}")
        print(f"  kept source (differing, region-tied class): {kept_src_diff}")
        print(f"  kept source (identical between regions): {kept_src_same}")
        print(f"  source-only entries (no USA equivalent): {not_in_usa}")

    write_afs(out_path, entries, toc_metadata=src_meta)
    if verbose:
        print(f"  wrote {out_path} ({out_path.stat().st_size:,} B)")
    return out_path


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["kr", "jp"], default="jp")
    args = ap.parse_args()
    build(source=args.source)
