"""Top-level catalog orchestration — extract every supported extension
in one pass."""
from __future__ import annotations

from pathlib import Path

from ._common import CATALOG_DIR
from .fpb import extract_all_fpb
from .region import REGION_OVERLAY_EXTS, extract_all_region
from .slot import SLOT_FORMATS, extract_all_slot


SUPPORTED_EXTS = (".fpb", *SLOT_FORMATS.keys(), *REGION_OVERLAY_EXTS)


def extract_all(usa_ship: Path,
                kr_ship: Path | None,
                jp_ship: Path | None,
                root_out_dir: Path = CATALOG_DIR
                ) -> dict[str, int]:
    """Extract catalogs for every supported extension into
    ``<root_out_dir>/<ext>/<basename>.json``. Returns ``{ext: count}``."""
    counts: dict[str, int] = {}
    counts[".fpb"] = extract_all_fpb(usa_ship, kr_ship, jp_ship,
                                     root_out_dir / "fpb")
    for ext in SLOT_FORMATS:
        counts[ext] = extract_all_slot(ext, usa_ship, kr_ship, jp_ship,
                                       root_out_dir / ext.lstrip("."))
    for ext in REGION_OVERLAY_EXTS:
        counts[ext] = extract_all_region(ext, usa_ship, kr_ship, jp_ship,
                                         root_out_dir / ext.lstrip("."))
    return counts
