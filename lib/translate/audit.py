"""Catalog audit — drives both ``translate-validate`` and ``translate-status``.

Walks every JSON catalog under ``translations/<ext>/`` and, for each, both:

  - **Validates** it against the corresponding USA bytes in SHIP.AFS, emitting
    one ``Issue`` per problem found (latin-1 errors, cap violations, edits to
    read-only fields, dropped ``$n`` tokens, mismatched structure).
  - **Counts** how many records have ``en`` differing from the USA original
    (= "edited"), so the translator can see at a glance how much work is done.

Single walk keeps the CLI snappy — re-extracting USA bytes per catalog is the
expensive part.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cri_afs import Afs

from translate._common import CATALOG_DIR
from translate.celfid import _read_celfid, _string_at
from translate.fpb import parse_fpb_raw, synthesize_implicit_seq0
from translate.region import REGION_OVERLAY_EXTS, find_text_regions
from translate.slot import SLOT_FORMATS, parse_slot_file, split_slot


# Engine-meaningful tokens the translator must preserve byte-for-byte.
# - "$n"     line break (most common, ~3000+ occurrences)
# - "$D<NN>" portrait/speaker/voice marker (14 records, all in .fpb)
_TOKEN_RE = re.compile(r"\$n|\$D\d+")


Level = Literal["error", "warn"]


@dataclass(frozen=True)
class Issue:
    catalog: Path        # path to the JSON catalog
    record: str          # e.g. "slot 5" / "region 3" / "data_section" / "header"
    field: str           # e.g. "en" / "cap" / "tail_at"
    level: Level
    message: str

    def format(self, *, root: Path | None = None) -> str:
        cat = self.catalog.relative_to(root) if root else self.catalog
        loc = f"{self.record}.{self.field}" if self.field else self.record
        tag = "ERROR" if self.level == "error" else "WARN "
        return f"  [{tag}] {cat} {loc}: {self.message}"


@dataclass
class ExtStatus:
    """Per-extension breakdown of catalog state."""
    total_records: int = 0          # records the build will see (slots/regions/data sections)
    edited_records: int = 0         # records whose en differs from USA
    files_total: int = 0            # JSON catalogs on disk
    files_with_edits: int = 0       # catalogs containing at least one edit


def _try_encode_latin1(s: str) -> tuple[bytes | None, str | None]:
    """Returns (encoded, error_msg). On success, error_msg is None."""
    try:
        return s.encode("latin-1"), None
    except UnicodeEncodeError as e:
        bad = s[e.start:e.end]
        return None, (
            f"non-latin-1 character {bad!r} at position {e.start} "
            f"(USA fonts can't render — use straight ASCII or accented Latin)"
        )


def _token_drop_warning(usa_en: str, cat_en: str) -> str | None:
    """Return a warning message if the catalog dropped any engine token
    that USA had, else None. Tokens (``$n`` / ``$D<NN>``) are runtime
    instructions the engine consumes — dropping them breaks line wrap,
    portrait switches, voice timing, etc."""
    usa_tokens = Counter(_TOKEN_RE.findall(usa_en))
    cat_tokens = Counter(_TOKEN_RE.findall(cat_en))
    missing = []
    for tok, n_usa in usa_tokens.items():
        n_cat = cat_tokens.get(tok, 0)
        if n_cat < n_usa:
            missing.append(f"{tok}×{n_usa - n_cat}")
    if not missing:
        return None
    return f"engine tokens dropped: {', '.join(missing)} (don't translate these — they're runtime instructions)"


# Game-derived dialog-box rendering constraints (measured from USA's text):
#   p99 line length between $n is 59 chars; the safe target is 50.
#   p99 $n count per record is 6. Beyond that the dialog box overflows.
_FPB_LINE_SOFT_MAX = 50    # warn above this
_FPB_LINE_HARD_MAX = 60    # past this it almost certainly overflows the box
_FPB_NL_SOFT_MAX = 6       # warn above this


def _fpb_layout_warnings(en: str) -> list[str]:
    """Return human-readable warnings about dialog-box layout, or []."""
    warns: list[str] = []
    # USA's text never starts or ends with $n; doing so causes the line break
    # to bleed into the adjacent dialog window's blank space.
    if en.startswith("$n"):
        warns.append("starts with $n — line break bleeds into the previous "
                     "dialog window; remove the leading $n")
    if en.endswith("$n"):
        warns.append("ends with $n — line break bleeds into the next dialog "
                     "window; remove the trailing $n")
    if re.search(r"(\$n){3,}", en):
        warns.append("contains 3+ consecutive $n tokens — collapse to at most "
                     "$n$n (one blank line)")
    # Strip $D<NN> markers — they're zero-width in render
    visible = re.sub(r"\$D\d+", "", en)
    # Drop angle-bracket UI labels — those render compact
    visible = re.sub(r"<[^>]+>", "", visible)
    # Lines = chunks between $n
    longest = max((len(line) for line in visible.split("$n")), default=0)
    if longest > _FPB_LINE_HARD_MAX:
        warns.append(
            f"longest line is {longest} chars (>{_FPB_LINE_HARD_MAX}) — "
            f"will overflow the dialog box; insert $n to wrap"
        )
    elif longest > _FPB_LINE_SOFT_MAX:
        warns.append(
            f"longest line is {longest} chars (target {_FPB_LINE_SOFT_MAX}) — "
            f"may overflow the dialog box on some scenes"
        )
    nl = en.count("$n")
    if nl > _FPB_NL_SOFT_MAX:
        warns.append(
            f"{nl} $n tokens (target {_FPB_NL_SOFT_MAX}) — "
            f"dialog box renders ~3-4 lines; extra $n likely overflow"
        )
    return warns


# --------------------------------------------------------------------------- format-specific audits


def _audit_fpb(catalog_path: Path, usa_blob: bytes,
               status: ExtStatus) -> list[Issue]:
    issues: list[Issue] = []
    try:
        cat = json.loads(catalog_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [Issue(catalog_path, "file", "", "error", f"invalid JSON: {e}")]

    try:
        _, usa_windows, usa_data = parse_fpb_raw(usa_blob)
    except ValueError as e:
        return [Issue(catalog_path, "file", "", "error",
                      f"USA bytes don't parse as .fpb: {e}")]
    # Catalogs include the implicit seq=0 record so every byte of the data
    # section is addressable; mirror that here for the comparison to line up.
    usa_windows = synthesize_implicit_seq0(usa_windows, data_len=len(usa_data))

    cat_records = cat.get("records")
    if not isinstance(cat_records, list):
        return [Issue(catalog_path, "file", "records",
                      "error",
                      "missing or non-list 'records' field — re-extract the catalog")]

    if len(cat_records) != len(usa_windows):
        issues.append(Issue(catalog_path, "records", "",
                            "error",
                            f"record count mismatch: catalog has {len(cat_records)}, "
                            f"USA has {len(usa_windows)} (don't add or remove records)"))

    file_edited = False
    for k, (rec, (seq, off, ln)) in enumerate(zip(cat_records, usa_windows)):
        loc = f"records[{k}]"
        usa_en = usa_data[off:off + ln].decode("latin-1", errors="replace")

        if rec.get("seq") != seq:
            issues.append(Issue(catalog_path, loc, "seq",
                                "error",
                                f"edited (read-only): {rec.get('seq')} != USA {seq}"))

        cat_en = rec.get("en", "")
        if not isinstance(cat_en, str):
            issues.append(Issue(catalog_path, loc, "en", "error",
                                f"expected string, got {type(cat_en).__name__}"))
            status.total_records += 1
            continue

        encoded, err = _try_encode_latin1(cat_en)
        if err is not None:
            issues.append(Issue(catalog_path, loc, "en", "error", err))
            status.total_records += 1
            continue

        if cat_en != usa_en:
            warn = _token_drop_warning(usa_en, cat_en)
            if warn is not None:
                issues.append(Issue(catalog_path, loc, "en", "warn", warn))
            for layout_warn in _fpb_layout_warnings(cat_en):
                issues.append(Issue(catalog_path, loc, "en", "warn", layout_warn))
            status.edited_records += 1
            file_edited = True
        status.total_records += 1

    if file_edited:
        status.files_with_edits += 1
    return issues


def _audit_slot(ext: str, catalog_path: Path, usa_blob: bytes,
                status: ExtStatus) -> list[Issue]:
    issues: list[Issue] = []
    try:
        cat = json.loads(catalog_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [Issue(catalog_path, "file", "", "error", f"invalid JSON: {e}")]

    if cat.get("ext") != ext:
        issues.append(Issue(catalog_path, "header", "ext",
                            "error",
                            f"wrong ext: {cat.get('ext')!r} != {ext!r}"))

    try:
        _, usa_slots = parse_slot_file(usa_blob, ext)
    except ValueError as e:
        return [Issue(catalog_path, "file", "", "error",
                      f"USA bytes don't parse as {ext}: {e}")]

    cat_slots = cat.get("slots", [])
    if len(cat_slots) != len(usa_slots):
        issues.append(Issue(catalog_path, "slots", "",
                            "error",
                            f"slot count mismatch: catalog {len(cat_slots)} vs USA {len(usa_slots)} "
                            f"(don't add or remove slots — re-extract if needed)"))

    file_edited = False
    for k, (usa_slot, cap) in enumerate(usa_slots):
        if k >= len(cat_slots):
            break
        rec = cat_slots[k]
        loc = f"slot[{k}]"
        usa_string, usa_tail_at, _ = split_slot(usa_slot, cap)

        cat_en = rec.get("en", "")
        if not isinstance(cat_en, str):
            issues.append(Issue(catalog_path, loc, "en", "error",
                                f"expected string, got {type(cat_en).__name__}"))
            status.total_records += 1
            continue

        encoded, err = _try_encode_latin1(cat_en)
        if err is not None:
            issues.append(Issue(catalog_path, loc, "en", "error", err))
            status.total_records += 1
            continue

        # Cap: must fit before the engine trailer (1 byte reserved for null)
        max_bytes = usa_tail_at - 1
        if len(encoded) > max_bytes:
            issues.append(Issue(
                catalog_path, loc, "en", "error",
                f"{len(encoded)} bytes > cap {max_bytes} "
                f"(trailer starts at byte {usa_tail_at}, 1 byte reserved for null)"
            ))

        usa_en = usa_string.decode("latin-1", errors="replace")
        if cat_en != usa_en:
            warn = _token_drop_warning(usa_en, cat_en)
            if warn is not None:
                issues.append(Issue(catalog_path, loc, "en", "warn", warn))
            status.edited_records += 1
            file_edited = True
        status.total_records += 1

    if file_edited:
        status.files_with_edits += 1
    return issues


def _audit_region(ext: str, catalog_path: Path, usa_blob: bytes,
                  status: ExtStatus) -> list[Issue]:
    issues: list[Issue] = []
    try:
        cat = json.loads(catalog_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [Issue(catalog_path, "file", "", "error", f"invalid JSON: {e}")]

    if cat.get("ext") != ext:
        issues.append(Issue(catalog_path, "header", "ext",
                            "error",
                            f"wrong ext: {cat.get('ext')!r} != {ext!r}"))

    usa_regions = find_text_regions(usa_blob)
    # Per-region capacity (matches the build path's _detect_regions)
    usa_caps: list[int] = []
    for k, (offset, length) in enumerate(usa_regions):
        next_start = usa_regions[k + 1][0] if k + 1 < len(usa_regions) else None
        # Inline the capacity walk — audit doesn't import region.py to avoid cycles
        end = next_start if next_start is not None else len(usa_blob)
        pos = offset + length
        while pos < end and usa_blob[pos] == 0:
            pos += 1
        usa_caps.append(pos - offset)

    cat_regions = cat.get("regions", [])
    if len(cat_regions) != len(usa_regions):
        issues.append(Issue(catalog_path, "regions", "",
                            "error",
                            f"region count mismatch: catalog {len(cat_regions)} vs USA {len(usa_regions)} "
                            f"(don't add or remove regions — re-extract if needed)"))

    file_edited = False
    for k, ((usa_off, usa_len), cap, rec) in enumerate(
            zip(usa_regions, usa_caps, cat_regions)):
        loc = f"region[{k}]"

        cat_en = rec.get("en", "")
        if not isinstance(cat_en, str):
            issues.append(Issue(catalog_path, loc, "en", "error",
                                f"expected string, got {type(cat_en).__name__}"))
            status.total_records += 1
            continue

        encoded, err = _try_encode_latin1(cat_en)
        if err is not None:
            issues.append(Issue(catalog_path, loc, "en", "error", err))
            status.total_records += 1
            continue

        if len(encoded) > cap:
            issues.append(Issue(
                catalog_path, loc, "en", "error",
                f"{len(encoded)} bytes > cap {cap} (trim the translation)"
            ))

        usa_en = usa_blob[usa_off : usa_off + usa_len].decode("latin-1", errors="replace")
        if cat_en != usa_en:
            warn = _token_drop_warning(usa_en, cat_en)
            if warn is not None:
                issues.append(Issue(catalog_path, loc, "en", "warn", warn))
            status.edited_records += 1
            file_edited = True
        status.total_records += 1

    if file_edited:
        status.files_with_edits += 1
    return issues


def _audit_celfid(catalog_path: Path, usa_file_afs: Path,
                  status: ExtStatus) -> list[Issue]:
    """Validate ``translations/celfid.json`` against USA's celfid.lix bytes."""
    issues: list[Issue] = []
    try:
        cat = json.loads(catalog_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return [Issue(catalog_path, "file", "", "error", f"invalid JSON: {e}")]

    if cat.get("file") != "celfid.lix":
        issues.append(Issue(catalog_path, "header", "file",
                            "error",
                            f"wrong file: {cat.get('file')!r} != 'celfid.lix'"))

    try:
        decomp = _read_celfid(usa_file_afs)
    except Exception as e:
        return [Issue(catalog_path, "file", "", "error",
                      f"can't read USA celfid.lix: {e}")]

    cat_slots = cat.get("slots", [])
    cat_groups = cat.get("linked_groups", [])
    file_edited = False

    def _check_one(loc: str, marker_hex, max_bytes, en_for_slot, usa_en):
        """Run the per-slot validation common to both stand-alones and
        group members. Returns the cap-violation flag; updates status."""
        nonlocal file_edited
        if not isinstance(marker_hex, str) or not isinstance(max_bytes, int):
            issues.append(Issue(catalog_path, loc, "marker/max_bytes",
                                "error",
                                "missing or wrong-type marker / max_bytes "
                                "(don't remove these — re-extract if needed)"))
            status.total_records += 1
            return
        try:
            marker = bytes.fromhex(marker_hex)
        except ValueError:
            issues.append(Issue(catalog_path, loc, "marker", "error",
                                "marker is not valid hex"))
            status.total_records += 1
            return
        n_matches = decomp.count(marker)
        if n_matches == 0:
            issues.append(Issue(catalog_path, loc, "marker", "error",
                                "marker not found in USA celfid.lix "
                                "(re-extract — USA bytes drifted?)"))
            status.total_records += 1
            return
        if n_matches > 1:
            issues.append(Issue(catalog_path, loc, "marker", "error",
                                f"marker matches {n_matches} positions in USA "
                                f"celfid.lix — not unique, patcher would land "
                                f"on the wrong slot. Re-extract."))
            status.total_records += 1
            return
        if not isinstance(en_for_slot, str):
            issues.append(Issue(catalog_path, loc, "en", "error",
                                f"expected string, got {type(en_for_slot).__name__}"))
            status.total_records += 1
            return
        encoded, err = _try_encode_latin1(en_for_slot)
        if err is not None:
            issues.append(Issue(catalog_path, loc, "en", "error", err))
            status.total_records += 1
            return
        cap = max_bytes - 1
        if len(encoded) > cap:
            issues.append(Issue(
                catalog_path, loc, "en", "error",
                f"{len(encoded)} bytes > cap {cap} "
                f"(slot's null-padded capacity is {max_bytes}; "
                f"1 byte reserved for terminator)"
            ))
        if en_for_slot != usa_en:
            warn = _token_drop_warning(usa_en, en_for_slot)
            if warn is not None:
                issues.append(Issue(catalog_path, loc, "en", "warn", warn))
            status.edited_records += 1
            file_edited = True
        status.total_records += 1

    # Stand-alone slots
    for k, rec in enumerate(cat_slots):
        marker = bytes.fromhex(rec.get("marker", "00")) if rec.get("marker") else b""
        idx = decomp.find(marker) if marker else -1
        usa_en = _string_at(decomp, idx + len(marker), encoding="latin-1")[0] if idx >= 0 else ""
        _check_one(f"slots[{k}]", rec.get("marker"), rec.get("max_bytes"),
                   rec.get("en", ""), usa_en)

    # Linked groups: each member's en is template.format(base=group_base),
    # so we validate against that derived value, not a per-slot en field.
    for gi, grp in enumerate(cat_groups):
        new_base = grp.get("base", "")
        for k, member in enumerate(grp.get("slots", [])):
            tmpl = member.get("template", "")
            try:
                derived_en = tmpl.format(base=new_base)
            except (KeyError, IndexError):
                issues.append(Issue(catalog_path,
                                    f"linked_groups[{gi}].slots[{k}]", "template",
                                    "error",
                                    f"template {tmpl!r} can't apply to base {new_base!r}"))
                status.total_records += 1
                continue
            marker = bytes.fromhex(member.get("marker", "00")) if member.get("marker") else b""
            idx = decomp.find(marker) if marker else -1
            usa_en = _string_at(decomp, idx + len(marker), encoding="latin-1")[0] if idx >= 0 else ""
            _check_one(f"linked_groups[{gi}][{grp.get('base')!r}].slots[{k}]",
                       member.get("marker"), member.get("max_bytes"),
                       derived_en, usa_en)

    if file_edited:
        status.files_with_edits += 1
    status.files_total = 1
    return issues


# --------------------------------------------------------------------------- entry point


def audit_all(usa_ship: Path,
              catalog_root: Path = CATALOG_DIR,
              usa_file_afs: Path | None = None
              ) -> tuple[list[Issue], dict[str, ExtStatus]]:
    """Walk every catalog under ``catalog_root``, audit each against USA SHIP
    (and ``celfid.lix`` inside USA FILE.AFS, when ``usa_file_afs`` is given),
    return (issues, per-extension status). Single pass over both."""
    usa = Afs.open(usa_ship)
    usa_n = usa.read_filename_toc() or []
    usa_idx = {n.lower(): i for i, n in enumerate(usa_n)}

    issues: list[Issue] = []
    status: dict[str, ExtStatus] = {}

    fpb_dir = catalog_root / "fpb"
    if fpb_dir.is_dir():
        st = ExtStatus()
        with usa_ship.open("rb") as fh:
            for cat_path in sorted(fpb_dir.glob("*.json")):
                st.files_total += 1
                ship_name = f"{cat_path.stem}.fpb"
                idx = usa_idx.get(ship_name.lower())
                if idx is None:
                    issues.append(Issue(cat_path, "file", "",
                                        "error",
                                        f"no USA SHIP entry named {ship_name!r}"))
                    continue
                usa_blob = usa.read_entry(idx, fh)
                issues.extend(_audit_fpb(cat_path, usa_blob, st))
        status[".fpb"] = st

    for ext in SLOT_FORMATS:
        sub = catalog_root / ext.lstrip(".")
        if not sub.is_dir():
            continue
        st = ExtStatus()
        with usa_ship.open("rb") as fh:
            for cat_path in sorted(sub.glob("*.json")):
                st.files_total += 1
                ship_name = f"{cat_path.stem}{ext}"
                idx = usa_idx.get(ship_name.lower())
                if idx is None:
                    issues.append(Issue(cat_path, "file", "",
                                        "error",
                                        f"no USA SHIP entry named {ship_name!r}"))
                    continue
                usa_blob = usa.read_entry(idx, fh)
                issues.extend(_audit_slot(ext, cat_path, usa_blob, st))
        status[ext] = st

    for ext in REGION_OVERLAY_EXTS:
        sub = catalog_root / ext.lstrip(".")
        if not sub.is_dir():
            continue
        st = ExtStatus()
        with usa_ship.open("rb") as fh:
            for cat_path in sorted(sub.glob("*.json")):
                st.files_total += 1
                ship_name = f"{cat_path.stem}{ext}"
                idx = usa_idx.get(ship_name.lower())
                if idx is None:
                    issues.append(Issue(cat_path, "file", "",
                                        "error",
                                        f"no USA SHIP entry named {ship_name!r}"))
                    continue
                usa_blob = usa.read_entry(idx, fh)
                issues.extend(_audit_region(ext, cat_path, usa_blob, st))
        status[ext] = st

    # celfid.lix (single catalog, lives at translations/celfid.json)
    celfid_cat = catalog_root / "celfid.json"
    if celfid_cat.exists() and usa_file_afs is not None and usa_file_afs.exists():
        st = ExtStatus()
        issues.extend(_audit_celfid(celfid_cat, usa_file_afs, st))
        status["celfid.lix"] = st

    return issues, status


# --------------------------------------------------------------------------- formatting helpers


def format_status_table(status: dict[str, ExtStatus]) -> str:
    """Render the per-extension status as an aligned plaintext table."""
    rows = [
        ("ext", "files (edited/total)", "records (edited/total)", "% edited"),
        ("---", "--------------------", "----------------------", "--------"),
    ]
    grand_files = grand_files_edit = grand_recs = grand_recs_edit = 0
    for ext in sorted(status):
        s = status[ext]
        pct = (100 * s.edited_records / s.total_records) if s.total_records else 0
        rows.append((
            ext,
            f"{s.files_with_edits}/{s.files_total}",
            f"{s.edited_records}/{s.total_records}",
            f"{pct:5.1f}%",
        ))
        grand_files += s.files_total
        grand_files_edit += s.files_with_edits
        grand_recs += s.total_records
        grand_recs_edit += s.edited_records
    pct = (100 * grand_recs_edit / grand_recs) if grand_recs else 0
    rows.append(("---", "--------------------", "----------------------", "--------"))
    rows.append((
        "TOTAL",
        f"{grand_files_edit}/{grand_files}",
        f"{grand_recs_edit}/{grand_recs}",
        f"{pct:5.1f}%",
    ))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    return "\n".join(
        "  " + "  ".join(c.ljust(widths[i]) for i, c in enumerate(row))
        for row in rows
    )
