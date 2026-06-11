r"""ISO9660 patcher for the Magna Carta undub.

Strategy
--------
Start from a copy of the original USA ISO. For each new cutscene SFD:

  - If the new file fits in its original slot (`new_size <= old_size`):
    write at the original LBA, pad trailing bytes with 0xFF, leave the
    ISO9660 directory entry alone.

  - If the new file is too large:
    append it to the end of the ISO (sector-aligned), patch the ISO9660
    directory entry to point at the new LBA + size, and grow the ISO's
    Primary Volume Descriptor `volume_space_size` to match.

The ISO byte layout for unchanged files is preserved exactly, so xdelta
against the original USA stays small (the diff is just per-file bytes +
patched directory entries + PVD size field).

Why this works
--------------
Magna Carta's boot ELF loads AFS files by ISO path (`cdrom0:\MUSIC.afs`),
not by hard-coded sector. Cutscenes are loaded the same way through CRI
SofDec. So as long as the ISO9660 directory still maps the correct path
to a valid extent, the engine finds the file.
"""
from __future__ import annotations

import shutil
import struct
import subprocess
from pathlib import Path

SECTOR = 0x800


def _read_iso_lbas(iso: Path) -> dict[str, tuple[int, int]]:
    """{full_iso_path: (lba, size_bytes)} for every regular file."""
    out = subprocess.check_output(["isoinfo", "-l", "-i", str(iso)], stderr=subprocess.DEVNULL)
    text = out.decode("latin-1")
    table: dict[str, tuple[int, int]] = {}
    cur_dir = "/"
    for line in text.splitlines():
        if line.startswith("Directory listing of "):
            cur_dir = line[len("Directory listing of "):].strip()
            continue
        if not line.startswith("-"):
            continue
        try:
            size = int(line.split()[4])
        except (IndexError, ValueError):
            continue
        l_open = line.find("[")
        l_close = line.find("]", l_open + 1)
        if l_open < 0 or l_close < 0:
            continue
        try:
            lba = int(line[l_open + 1: l_close].split()[0])
        except (IndexError, ValueError):
            continue
        name = line[l_close + 1:].strip()
        if name.endswith(";1"):
            name = name[:-2]
        full = cur_dir.rstrip("/") + "/" + name
        table[full] = (lba, size)
    return table


def _dir_lba_size(iso: Path, dir_path: str) -> tuple[int, int]:
    """Locate the on-disc LBA and byte size of a directory record.

    `dir_path` should end in `/` and use ISO9660 paths (e.g. `/`, `/MOVIE18/`).
    """
    out = subprocess.check_output(["isoinfo", "-l", "-i", str(iso)], stderr=subprocess.DEVNULL)
    text = out.decode("latin-1")
    cur = None
    for line in text.splitlines():
        if line.startswith("Directory listing of "):
            cur = line[len("Directory listing of "):].strip()
        if cur != dir_path:
            continue
        if line.startswith("d") and " . " in line:
            l_open = line.find("[")
            l_close = line.find("]", l_open + 1)
            try:
                lba = int(line[l_open + 1: l_close].split()[0])
                size = int(line.split()[4])
                return lba, size
            except (IndexError, ValueError):
                pass
    raise KeyError(f"directory {dir_path!r} not found in {iso}")


def _patch_dir_entry(directory_blob: bytearray, name_with_ver: str,
                     new_lba: int, new_size: int) -> bool:
    """Walk an ISO9660 directory blob and patch the (lba, size) fields of the
    entry whose name matches `name_with_ver` (e.g. "180101.SFD;1"). Returns
    True if a record was patched.
    """
    target = name_with_ver.encode("ascii")
    pos = 0
    n = len(directory_blob)
    while pos < n:
        rec_len = directory_blob[pos]
        if rec_len == 0:
            # padding to next sector boundary
            pos = (pos // SECTOR + 1) * SECTOR
            continue
        name_len = directory_blob[pos + 32]
        name = bytes(directory_blob[pos + 33: pos + 33 + name_len])
        if name == target:
            # extent location: u32 LE at +2, u32 BE at +6
            directory_blob[pos + 2: pos + 6] = struct.pack("<I", new_lba)
            directory_blob[pos + 6: pos + 10] = struct.pack(">I", new_lba)
            # data length: u32 LE at +10, u32 BE at +14
            directory_blob[pos + 10: pos + 14] = struct.pack("<I", new_size)
            directory_blob[pos + 14: pos + 18] = struct.pack(">I", new_size)
            return True
        pos += rec_len
    return False


def _patch_pvd_volume_size(iso_path: Path, new_total_sectors: int) -> None:
    """Update the Primary Volume Descriptor's volume_space_size to reflect
    a grown ISO. PVD lives at LBA 16, the field is at offset 80 (both-endian)."""
    with iso_path.open("r+b") as fh:
        fh.seek(16 * SECTOR + 80)
        fh.write(struct.pack("<I", new_total_sectors))
        fh.write(struct.pack(">I", new_total_sectors))


def patch_iso(src_iso: Path, out_iso: Path, replacements: dict[str, Path],
              verbose: bool = True) -> tuple[int, int]:
    """Patch the ISO with new cutscene contents.

    `replacements` maps full ISO paths (like '/MOVIE18/180101.SFD') to local
    files containing the new bytes. Returns (in_place_count, relocated_count).
    """
    if out_iso.exists():
        out_iso.unlink()
    shutil.copy2(src_iso, out_iso)
    lba_table = _read_iso_lbas(src_iso)

    # Group replacements by parent directory so we batch-patch each directory
    # blob once (saves rewriting the dir for every relocation in it).
    by_dir: dict[str, list[tuple[str, Path]]] = {}
    for iso_path, new_file in replacements.items():
        stripped = iso_path.strip("/")
        parent = ("/" + stripped.rsplit("/", 1)[0]) if "/" in stripped else "/"
        by_dir.setdefault(parent, []).append((iso_path, new_file))

    # Find current ISO end (in sectors) so we know where to append.
    src_sectors = src_iso.stat().st_size // SECTOR
    next_append_lba = src_sectors

    in_place = 0
    relocated = 0
    dir_patches: dict[str, tuple[int, bytearray]] = {}

    with out_iso.open("r+b") as fh:
        for parent, jobs in by_dir.items():
            dir_query = "/" if parent == "/" else parent + "/"
            dir_lba, dir_size = _dir_lba_size(src_iso, dir_query)
            fh.seek(dir_lba * SECTOR)
            blob = bytearray(fh.read(dir_size))

            for iso_path, new_file in jobs:
                old_lba, old_size = lba_table[iso_path]
                data = new_file.read_bytes()
                name_only = iso_path.rsplit("/", 1)[1] + ";1"

                if len(data) <= old_size:
                    # in-place write, pad slot to slot_size with 0xFF
                    fh.seek(old_lba * SECTOR)
                    fh.write(data)
                    fh.write(b"\xff" * (old_size - len(data)))
                    # Patch the directory entry so isoinfo / engine reads
                    # the actual file size, not the padded slot size.
                    if (len(data) != old_size
                            and not _patch_dir_entry(blob, name_only, old_lba,
                                                     len(data))):
                        raise RuntimeError(f"could not find dir entry for {iso_path}")
                    in_place += 1
                    if verbose:
                        print(f"  [in-place] {iso_path}  {len(data):,} B (slot {old_size:,})")
                else:
                    # relocate: append past the current ISO end
                    new_lba = next_append_lba
                    fh.seek(new_lba * SECTOR)
                    fh.write(data)
                    pad = (-len(data)) % SECTOR
                    if pad:
                        fh.write(b"\x00" * pad)
                    next_append_lba += (len(data) + pad) // SECTOR
                    if not _patch_dir_entry(blob, name_only, new_lba, len(data)):
                        raise RuntimeError(f"could not find dir entry for {iso_path}")
                    # zero out the original slot so leftover USA bytes can't
                    # confuse anything that reads from the old LBA.
                    fh.seek(old_lba * SECTOR)
                    fh.write(b"\xff" * old_size)
                    relocated += 1
                    if verbose:
                        print(f"  [reloc]    {iso_path}  {len(data):,} B  "
                              f"old slot {old_size:,}  -> LBA {new_lba}")

            dir_patches[parent] = (dir_lba, blob)

        # Write back patched directories
        for dir_lba, blob in dir_patches.values():
            fh.seek(dir_lba * SECTOR)
            fh.write(bytes(blob))

    # Grow PVD if we relocated anything past the original ISO end
    if next_append_lba > src_sectors:
        _patch_pvd_volume_size(out_iso, next_append_lba)
        # Truncate to the new size in case shutil.copy2 left tail bytes
        with out_iso.open("r+b") as fh:
            fh.truncate(next_append_lba * SECTOR)
        if verbose:
            print(f"  PVD volume_space_size -> {next_append_lba} sectors "
                  f"({next_append_lba * SECTOR:,} bytes)")

    return in_place, relocated


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--map", type=Path, required=True,
                    help="text file: each line is `<iso_path>\\t<local_file>`")
    args = ap.parse_args()
    repl: dict[str, Path] = {}
    for line in args.map.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        iso_p, local = line.split("\t", 1)
        repl[iso_p] = Path(local)
    inp, rel = patch_iso(args.src, args.out, repl)
    print(f"\nin-place: {inp}, relocated: {rel}, ISO: {args.out}")
