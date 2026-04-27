"""Classify each LINEAR.AFS .lin file by content type.

Each .lin is a zlib-compressed UE2 level package (.unr). When decompressed,
we can count UE2 export classes to determine whether the package is
texture-heavy (likely a UI/menu screen safe to swap to USA) or actor/
geometry-heavy (likely a gameplay scene that must stay KR for KR `.fld`
references to resolve).

For the 124 LINEAR `.lin` files that differ in size between regions, we
classify each and decide individually whether to swap to USA.
"""
from __future__ import annotations

import hashlib
import re
import struct
import sys
import zlib
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from afs import Afs


def decompress_full(data: bytes) -> bytes:
    """Walk concatenated zlib streams in a `.lin` file (header is 8 bytes)."""
    out = bytearray()
    pos = 8  # skip 4-byte magic + 4-byte size header
    while pos < len(data) - 2:
        if data[pos:pos + 2] == b"\x78\x01":
            try:
                d = zlib.decompressobj()
                chunk = d.decompress(data[pos:])
                out += chunk
                if d.unused_data:
                    pos = len(data) - len(d.unused_data)
                else:
                    break
            except Exception:
                pos += 1
        else:
            pos += 1
    return bytes(out)


# Heuristics: how to identify "texture-heavy" levels.
# UE2 packages contain a name table where class names are listed. The
# decompressed .lin contains literals like b"Texture", b"PointRegion",
# b"PhysicsVolume", b"Actor", b"PlayerStart", b"StaticMeshActor", etc.
#
# A "menu/UI screen" .lin tends to:
#   - have many Texture name occurrences (UI button textures, backgrounds)
#   - have FEW Actor / PhysicsVolume / Region / NavigationPoint references
# A "gameplay scene" .lin tends to have many Actor/Volume/Region references.


def texture_score(blob: bytes) -> dict:
    """Return counts of UE2 class-name appearances that distinguish texture-
    bearing UI screens from gameplay levels."""
    counts = {
        "Texture": len(re.findall(rb"\bTexture\b", blob)),
        "Material": len(re.findall(rb"\bMaterial\b", blob)),
        "Actor": len(re.findall(rb"\bActor\b", blob)),
        "PhysicsVolume": len(re.findall(rb"\bPhysicsVolume\b", blob)),
        "Region": len(re.findall(rb"\bRegion\b", blob)),
        "PointRegion": len(re.findall(rb"\bPointRegion\b", blob)),
        "NavigationPoint": len(re.findall(rb"\bNavigationPoint\b", blob)),
        "PlayerStart": len(re.findall(rb"\bPlayerStart\b", blob)),
        "StaticMesh": len(re.findall(rb"\bStaticMesh\b", blob)),
        "Light": len(re.findall(rb"\bLight\b", blob)),
        "Pawn": len(re.findall(rb"\bPawn\b", blob)),
    }
    return counts


def classify_one(usa_data: bytes, kr_data: bytes) -> dict:
    """Decompress each, score, decide. 'texture_heavy' = USA-swappable."""
    u = decompress_full(usa_data)
    k = decompress_full(kr_data)
    us = texture_score(u)
    ks = texture_score(k)
    actor_score_usa = us["Actor"] + us["PhysicsVolume"] + us["Region"] + us["NavigationPoint"] + us["PlayerStart"] + us["Pawn"]
    texture_score_usa = us["Texture"] + us["Material"]
    # Heuristic: texture-heavy if it has textures and very few actors.
    is_texture_heavy = (
        texture_score_usa >= 3
        and actor_score_usa <= 2
    )
    return {
        "usa_decompressed_bytes": len(u),
        "kr_decompressed_bytes": len(k),
        "usa_scores": us,
        "kr_scores": ks,
        "is_texture_heavy": is_texture_heavy,
    }


def main() -> int:
    usa = Afs.open(ROOT / "work/usa/LINEAR.AFS"); usa_n = usa.read_filename_toc()
    kr = Afs.open(ROOT / "work/kr/LINEAR.AFS");  kr_n = kr.read_filename_toc()
    ui = {n.lower(): i for i, n in enumerate(usa_n)}
    ki = {n.lower(): i for i, n in enumerate(kr_n)}

    diffsize_pairs: list[tuple[str, int, int]] = []
    samesize_diff: list[tuple[str, int]] = []
    with open(ROOT / "work/usa/LINEAR.AFS", "rb") as fu, open(ROOT / "work/kr/LINEAR.AFS", "rb") as fk:
        for n in sorted(set(ui) & set(ki)):
            uo = usa.entries[ui[n]]; ko = kr.entries[ki[n]]
            if uo.size != ko.size:
                diffsize_pairs.append((n, uo.size, ko.size))
            else:
                ud = usa.read_entry(ui[n], fu); kd = kr.read_entry(ki[n], fk)
                if hashlib.sha1(ud).digest() != hashlib.sha1(kd).digest():
                    samesize_diff.append((n, uo.size))

    # for the cases that differ, decompress + classify each
    candidates = diffsize_pairs + [(n, sz, sz) for n, sz in samesize_diff]
    print(f"classifying {len(candidates)} differing .lin files ...")

    texture_heavy_names: list[str] = []
    actor_heavy_names: list[str] = []
    with open(ROOT / "work/usa/LINEAR.AFS", "rb") as fu, open(ROOT / "work/kr/LINEAR.AFS", "rb") as fk:
        for n, us, ks in candidates:
            ud = usa.read_entry(ui[n], fu)
            kd = kr.read_entry(ki[n], fk)
            try:
                cls = classify_one(ud, kd)
            except Exception as e:
                print(f"  FAIL {n}: {e}")
                continue
            label = "TEX" if cls["is_texture_heavy"] else "ACT"
            print(f"  [{label}] {n:<22} USA={us:>9,} KR={ks:>9,} "
                  f"Tex/Mat={cls['usa_scores']['Texture']}/{cls['usa_scores']['Material']} "
                  f"Act={cls['usa_scores']['Actor']} "
                  f"Volumes={cls['usa_scores']['PhysicsVolume']} ")
            if cls["is_texture_heavy"]:
                texture_heavy_names.append(n)
            else:
                actor_heavy_names.append(n)

    print()
    print(f"texture-heavy (USA-swappable): {len(texture_heavy_names)}")
    print(f"actor-heavy   (must stay KR):  {len(actor_heavy_names)}")
    out = ROOT / "build" / "lin_classification.json"
    import json
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "texture_heavy": texture_heavy_names,
        "actor_heavy": actor_heavy_names,
    }, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
