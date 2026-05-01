"""Shared helpers used by every format-family submodule.

The retranslation pipeline writes per-file JSON catalogs under
``translations/<ext>/<basename>.json``. Each format module reads/writes
those catalogs but they all share three things: where the catalog root
lives, how to encode/decode strings safely, and a small AFS open helper.
"""
from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from cri_afs import Afs

ROOT = Path(__file__).resolve().parents[2]
CATALOG_DIR = ROOT / "translations"


def encode_en(s: str) -> bytes:
    """Translator's English string -> bytes via latin-1 (lossless 0x00–0xFF).

    Rejects characters USA fonts can't render (anything outside latin-1,
    e.g. emoji or smart quotes) so the failure is loud at build time
    rather than silent at runtime.
    """
    try:
        return s.encode("latin-1")
    except UnicodeEncodeError as e:
        raise ValueError(
            f"English text contains a character USA fonts can't render "
            f"(latin-1 only): {e}"
        ) from e


def decode_safe(b: bytes, encoding: str) -> str:
    """Best-effort decode — non-decodable bytes become the replacement
    character. Used for source-region (KR/JP) reference text in catalogs."""
    return b.decode(encoding, errors="replace")


def open_ship_handles(usa_ship: Path,
                      kr_ship: Path | None,
                      jp_ship: Path | None
                      ) -> tuple[Afs, dict[str, int],
                                 tuple[Afs, dict[str, int]] | None,
                                 tuple[Afs, dict[str, int]] | None,
                                 dict[str, BinaryIO]]:
    """Open USA + optional KR/JP SHIP archives plus their TOC indices.

    Returned tuple:
      (usa_afs, usa_idx, kr_pair, jp_pair, fhs)
    where each ``*_pair`` is ``(afs, idx)`` or None, and ``fhs`` is a
    dict of region -> open file handles. Caller is responsible for
    closing the file handles in fhs.
    """
    usa = Afs.open(usa_ship)
    usa_idx = {n.lower(): i for i, n in enumerate(usa.read_filename_toc())}
    fhs: dict[str, BinaryIO] = {"usa": usa_ship.open("rb")}

    kr_pair = None
    if kr_ship and kr_ship.exists():
        kr = Afs.open(kr_ship)
        kr_idx = {n.lower(): i for i, n in enumerate(kr.read_filename_toc())}
        kr_pair = (kr, kr_idx)
        fhs["kr"] = kr_ship.open("rb")

    jp_pair = None
    if jp_ship and jp_ship.exists():
        jp = Afs.open(jp_ship)
        jp_idx = {n.lower(): i for i, n in enumerate(jp.read_filename_toc())}
        jp_pair = (jp, jp_idx)
        fhs["jp"] = jp_ship.open("rb")

    return usa, usa_idx, kr_pair, jp_pair, fhs
