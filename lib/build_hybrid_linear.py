"""Hybrid LINEAR.AFS: KR base (scene structure intact) with USA `.lin`
swapped in for files classified as texture-heavy by lib/lin_classifier.py.

The classifier identified ~92 differing `.lin` files dominated by UE2
Texture/Material exports (UI menus, item icons, etc., where USA has English
glyphs baked into the textures). The remaining ~34 differing `.lin` files
are scene scripts with KR-specific actor/volume references; they MUST
stay KR or D13's "broken on new game" symptom returns.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from afs import Afs, write_afs


def build(out_path: Path = ROOT / "build" / "hybrid" / "LINEAR.AFS",
          usa_linear: Path = ROOT / "work" / "usa" / "LINEAR.AFS",
          kr_linear: Path = ROOT / "work" / "kr" / "LINEAR.AFS",
          classification_json: Path = ROOT / "build" / "lin_classification.json",
          verbose: bool = True) -> Path:
    """KR-base LINEAR.AFS with texture-heavy .lin files swapped to USA."""
    import json
    cls = json.loads(classification_json.read_text())
    texture_heavy = set(cls["texture_heavy"])

    usa = Afs.open(usa_linear); usa_n = usa.read_filename_toc()
    kr = Afs.open(kr_linear);  kr_n = kr.read_filename_toc()
    usa_idx = {n.lower(): i for i, n in enumerate(usa_n)}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    entries: list[tuple[str, bytes]] = []
    swapped_usa = 0
    kept_kr = 0

    with usa_linear.open("rb") as fu, kr_linear.open("rb") as fk:
        for i, name in enumerate(kr_n):
            ln = name.lower()
            if ln in texture_heavy and ln in usa_idx:
                blob = usa.read_entry(usa_idx[ln], fu)
                swapped_usa += 1
            else:
                blob = kr.read_entry(i, fk)
                kept_kr += 1
            entries.append((name, blob))

    if verbose:
        print(f"  total entries (KR's count): {len(entries)}")
        print(f"  USA-swapped (texture-heavy): {swapped_usa}")
        print(f"  KR kept:                     {kept_kr}")

    # Pass through KR's TOC metadata (entry list and order match KR exactly)
    kr_meta = kr.read_toc_metadata()
    write_afs(out_path, entries, toc_metadata=kr_meta)
    if verbose:
        print(f"  wrote {out_path} ({out_path.stat().st_size:,} B)")
    return out_path


if __name__ == "__main__":
    build()
