"""Shared core for the hybrid AFS builders (SHIP.AFS, LINEAR.AFS).

Both undub archives are built the same way: take a source region's AFS as
the structural base, overlay USA bytes for the files that hold
language-specific content, then **rebuild the slot-0 plaintext manifest** so
its ``(filename, size)`` table matches the bytes we actually wrote.

That manifest rebuild is the keystone of the whole undub. The engine reads
slot 0 — ``AFSShipFileIndex.idx`` / ``AFSLINEARFileIndex.idx`` — at boot to
populate its file-size cache, *not* the AFS primary TOC. Leave the manifest
pointing at the source region's sizes and any USA-bigger overlay gets read
short, overrunning the parser's buffer.

The per-archive overlay rules differ and live in their own modules
(``ship.py`` swaps text extensions; ``linear.py`` swaps Texture/StaticMesh
``.lin`` packages). This module owns the two things they share verbatim: the
manifest format and the finalize step (manifest rebuild + source-TOC
passthrough + write).
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from cri_afs import Afs, write_afs

Entry = tuple[str, bytes]


def filename_toc(afs: Afs) -> list[str]:
    """The archive's filename TOC, required. Every archive this pipeline
    touches (SHIP/LINEAR/FILE/MOVIE) has one; ``None`` would mean a foreign
    or corrupt AFS, so fail loudly instead of propagating an Optional."""
    names = afs.read_filename_toc()
    if names is None:
        raise ValueError("AFS has no filename TOC — not a Magna Carta archive?")
    return names


def build_manifest(entries: list[Entry], *, manifest_name: str,
                   header: str, strip_ext: str | None = None) -> bytes:
    r"""Build a slot-0 manifest from `entries`' actual sizes.

    Plaintext, CRLF-delimited, ASCII::

        <header>\r\n
        0\r\n                    <- self-size placeholder (the engine reads
                                    this entry's real size from the AFS TOC)
        <name1>\r\n
        <size1_in_decimal>\r\n
        ...

    `header` is the first body line — SHIP keeps the ``.idx`` suffix, LINEAR
    drops it. `manifest_name` is the slot-0 entry that's skipped while
    listing. `strip_ext`, when set (e.g. ``".lin"``), is removed from each
    listed name to match that archive's original manifest convention.
    """
    lines = [header, "0"]
    for name, blob in entries:
        if name == manifest_name:
            continue
        if strip_ext and name.lower().endswith(strip_ext):
            name = name[: -len(strip_ext)]
        lines.append(name)
        lines.append(str(len(blob)))
    return ("\r\n".join(lines) + "\r\n").encode("ascii")


def finalize_hybrid_afs(out_path: Path, src: Afs, entries: list[Entry], *,
                        manifest_name: str, header: str,
                        strip_ext: str | None = None,
                        verbose: bool = True,
                        report: Callable[[], None] | None = None) -> Path:
    """Rebuild slot 0 from the actual sizes, then write the hybrid archive.

    `entries` must start with the source region's slot-0 manifest — it's
    replaced in place with one sized to the bytes we're about to write. `src`
    is the source-region :class:`Afs`; its TOC metadata is passed through
    unchanged, which is valid because the entry list and order are identical
    to the source base, so its 16-byte per-entry trailers still align.

    `report`, when given, is called (only if `verbose`) at the same point the
    builders historically logged their per-archive stats: after the manifest
    line, before the write.
    """
    assert entries[0][0] == manifest_name, (
        f"slot 0 should be {manifest_name}, got {entries[0][0]}"
    )
    new_manifest = build_manifest(entries, manifest_name=manifest_name,
                                  header=header, strip_ext=strip_ext)
    if verbose:
        print(f"  manifest: source-region original {src.entries[0].size:,} B → "
              f"rebuilt {len(new_manifest):,} B")
    entries[0] = (manifest_name, new_manifest)

    src_meta = src.read_toc_metadata()
    if verbose and report is not None:
        report()
    write_afs(out_path, entries, toc_metadata=src_meta)
    if verbose:
        print(f"  wrote {out_path} ({out_path.stat().st_size:,} B)")
    return out_path


def rebuild_afs(src_afs_path: Path, out_path: Path,
                overrides: dict[str, bytes]) -> Path:
    """Rewrite an AFS, swapping named entries for new bytes.

    `overrides` maps a lowercase entry filename to its replacement bytes;
    every other entry is copied through unchanged and the source TOC
    metadata is passed through verbatim. That passthrough is valid only
    because the entry list, order, and count are identical to the source —
    this is a pure content swap, not a structural rebuild.

    Use it for single-file overlays into archives with no slot-0 manifest
    (e.g. FILE.AFS's celfid.lix). SHIP/LINEAR, whose manifest is keyed to
    file sizes, must go through :func:`finalize_hybrid_afs` instead.
    """
    src = Afs.open(src_afs_path)
    names = filename_toc(src)
    meta = src.read_toc_metadata()
    entries: list[Entry] = []
    with src_afs_path.open("rb") as fh:
        for i, name in enumerate(names):
            blob = overrides.get(name.lower())
            entries.append((name, blob if blob is not None
                            else src.read_entry(i, fh)))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_afs(out_path, entries, toc_metadata=meta)
    return out_path
