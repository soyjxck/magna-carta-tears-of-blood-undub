"""Retranslation pipeline for SHIP.AFS text files.

Per-file JSON catalogs under ``translations/<ext>/<basename>.json``,
opt-in via ``patch.py build-iso --translations``. Default builds use
raw USA bytes (vanilla undub).

Three format families covered:

  * **fpb** — windowed-pool dialog (records are ``(seq, offset, length)``
    windows into a shared text section). Catalog stores the whole
    section as one editable string; rebuild diffs old↔new and remaps
    every window. See ``fpb.py``.

  * **slot** — fixed-stride slot files. ``.cht .odd .gft .cha .cdg
    .mdg .ecd .fds`` (76 files). Each slot is one null-terminated
    string with optional engine trailer. See ``slot.py``.

  * **region** — region-overlay files. ``.pod .tui .itm .abi .sgi .nod
    .dod .cls .att .val`` (208 files). Find ASCII runs in binary,
    overwrite them on rebuild while preserving everything else
    verbatim. See ``region.py``.

The line-break token is the literal ASCII sequence ``$n``. USA fonts
only render latin-1 characters.
"""
from __future__ import annotations

# Public API — keep stable for callers in lib/ship.py and patch.py.
from translate._common import CATALOG_DIR
from translate.audit import ExtStatus, Issue, audit_all, format_status_table
from translate.catalog import SUPPORTED_EXTS, extract_all
from translate.celfid import (
                              build_file_afs_with_celfid,
                              extract_celfid_catalog,
                              translated_celfid_bytes,
)
from translate.fpb import (
                              build_fpb,
                              extract_all_fpb,
                              parse_fpb_raw,
                              remap_windows,
                              synthesize_implicit_seq0,
                              translated_fpb_bytes,
)
from translate.region import (
                              REGION_OVERLAY_EXTS,
                              extract_all_region,
                              find_text_regions,
                              translated_region_bytes,
)
from translate.slot import (
                              SLOT_FORMATS,
                              build_slot_file,
                              extract_all_slot,
                              parse_slot_file,
                              split_slot,
                              translated_slot_bytes,
)

__all__ = [
    "CATALOG_DIR", "SUPPORTED_EXTS", "SLOT_FORMATS", "REGION_OVERLAY_EXTS",
    "extract_all", "extract_all_fpb", "extract_all_slot", "extract_all_region",
    "translated_fpb_bytes", "translated_slot_bytes", "translated_region_bytes",
    "parse_fpb_raw", "build_fpb", "remap_windows", "synthesize_implicit_seq0",
    "parse_slot_file", "build_slot_file", "split_slot",
    "find_text_regions",
    "audit_all", "format_status_table", "ExtStatus", "Issue",
    "extract_celfid_catalog", "translated_celfid_bytes",
    "build_file_afs_with_celfid",
]
