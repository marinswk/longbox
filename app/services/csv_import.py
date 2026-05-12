"""CSV → list-of-rows parser + column-map autosuggester for the wizard.

Quirks handled:
  * UTF-8 BOM in the first cell of the header row (`utf-8-sig` decode).
  * Trailing whitespace in headers.
  * Leading empty column (off-by-one — Star Wars Canon CSV in the wild).
  * Comma / tab / semicolon separators (auto-detect by first-line column count).
  * Section-header rows (only one cell populated — typically the `Fandom`
    column) acting as visual dividers — skipped, but counted.
  * Fully-empty rows — skipped, counted.

Returns a `ParsedCSV` with:
  * `headers`: original headers, in order, with leading empties dropped
  * `rows`: list[dict] with keys = original headers, values = stripped strings
  * `skipped_section`: int — count of section-header rows
  * `skipped_empty`:   int — count of all-blank rows

The downstream column-map step normalizes headers to our internal field
names; this parser keeps original-case headers so the user can recognize them.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from typing import Iterable

# Separators tried in order. CSV is the dominant case; we fall through if
# the first line splits into <2 columns.
_SEPARATORS = [",", "\t", ";"]


@dataclass
class ParsedCSV:
    headers: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    skipped_section: int = 0
    skipped_empty: int = 0
    # Diagnostics: which separator + encoding worked.
    separator: str = ","
    encoding: str = "utf-8-sig"


def _decode(blob: bytes) -> str:
    """Decode bytes as UTF-8 with BOM tolerance. Falls back to latin-1
    so we never crash on weird user input — content might be slightly
    mangled but the wizard can still progress."""
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return blob.decode(enc)
        except UnicodeDecodeError:
            continue
    return blob.decode("latin-1", errors="replace")


def _pick_separator(text: str) -> str:
    """Use the first non-empty line to guess. Tab and semicolon are real
    in some European spreadsheet exports; comma is the default."""
    sample = next((ln for ln in text.splitlines() if ln.strip()), "")
    if not sample:
        return ","
    counts = {sep: sample.count(sep) for sep in _SEPARATORS}
    # Pick the separator with the most occurrences, breaking ties with the
    # _SEPARATORS order (so comma wins ties).
    best = max(_SEPARATORS, key=lambda s: (counts[s], -_SEPARATORS.index(s)))
    return best if counts[best] > 0 else ","


def _normalize_headers(raw_headers: Iterable[str]) -> tuple[list[str], int]:
    """Strip whitespace + drop leading empty headers (so a CSV with an
    accidental leading column lines up properly). Returns the cleaned
    list AND the offset (how many leading empties were dropped) so the
    row data can be re-aligned.
    """
    headers = [(h or "").strip() for h in raw_headers]
    offset = 0
    while headers and headers[0] == "":
        headers.pop(0)
        offset += 1
    return headers, offset


def _is_section_row(values: list[str]) -> bool:
    """A row with at most one populated cell is a section header (e.g.
    "Aggretsuko" sitting alone in the Fandom column). Treat as a divider."""
    populated = sum(1 for v in values if (v or "").strip())
    return populated <= 1 and populated >= 1  # one populated; zero is "empty"


def _is_empty_row(values: list[str]) -> bool:
    return not any((v or "").strip() for v in values)


def parse_csv(blob: bytes) -> ParsedCSV:
    """Top-level entry point: bytes from an upload → ParsedCSV.

    Never raises on malformed input; failures degrade to "fewer rows
    parsed". The wizard's UI will tell the user how many were dropped.
    """
    text = _decode(blob)
    if not text.strip():
        return ParsedCSV()

    sep = _pick_separator(text)
    reader = csv.reader(io.StringIO(text), delimiter=sep)
    try:
        raw_headers = next(reader)
    except StopIteration:
        return ParsedCSV()

    headers, offset = _normalize_headers(raw_headers)
    if not headers:
        return ParsedCSV(separator=sep)

    parsed = ParsedCSV(headers=headers, separator=sep)
    for raw_row in reader:
        # Strip the leading empties to match the header offset.
        values = list(raw_row)[offset:]

        if _is_empty_row(values):
            parsed.skipped_empty += 1
            continue
        if _is_section_row(values):
            parsed.skipped_section += 1
            continue

        # Build a dict aligned to the cleaned headers. Excess columns are
        # discarded; missing columns become empty string.
        row_dict: dict[str, str] = {}
        for i, header in enumerate(headers):
            cell = values[i] if i < len(values) else ""
            row_dict[header] = (cell or "").strip()
        parsed.rows.append(row_dict)

    return parsed


# ---------------------------------------------------------------------------
# Column mapping (step 2 of the wizard)
# ---------------------------------------------------------------------------


@dataclass
class TargetField:
    key: str          # internal name written into ImportRow.mapped JSON
    label: str        # human label shown in the mapping form
    aliases: tuple[str, ...]  # normalized header variants to autosuggest from
    hint: str = ""    # optional helper text under the dropdown


# Targets the wizard knows how to consume. Order matters — it's the order
# the form is rendered. New target fields go at the bottom.
OUR_FIELDS: tuple[TargetField, ...] = (
    TargetField("series", "Series",
                ("series", "series name", "seriesname", "series_title")),
    TargetField("title", "Title",
                ("title", "issue title", "name")),
    TargetField("issue_number", "Issue number",
                ("issue number", "issuenumber", "issue", "issue#",
                 "issue no", "issue_no", "number")),
    TargetField("year", "Series year",
                ("series year", "seriesyear", "year", "release year",
                 "publication year")),
    TargetField("publisher", "Publisher",
                ("publisher", "imprint")),
    TargetField("format", "Type / format",
                ("type", "format", "binding"),
                hint="Common values: TPB · HC · OMNIBUS · SINGLE_ISSUE · OGN"),
    TargetField("collected_issues", "Collected issues",
                ("collected issues", "collected_issues", "collects",
                 "contains", "trade contents")),
    TargetField("variant", "Variant",
                ("variant", "edition", "cover variant")),
    TargetField("fandom", "Fandom",
                ("fandom", "universe", "franchise"),
                hint="Each comic gets `comic.fandom` set to this value."),
    TargetField("isbn_13", "ISBN-13",
                ("isbn", "isbn 13", "isbn13", "isbn-13", "isbn_13")),
    TargetField("upc", "UPC / barcode",
                ("upc", "barcode", "ean", "sku")),
)

OUR_FIELD_KEYS: tuple[str, ...] = tuple(f.key for f in OUR_FIELDS)


def _normalize_header(s: str) -> str:
    """Lowercase, strip non-alphanumerics, collapse spaces. Used to match
    a CSV header against a `TargetField.aliases` list robustly."""
    s = (s or "").strip().lower()
    s = re.sub(r"[\-_/\.]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def suggest_mapping(headers: Iterable[str]) -> dict[str, str]:
    """Return `{our_key: csv_header}` for fields whose autosuggest matched.

    Matching is greedy in target order: the first CSV header that hits any
    alias of an OUR_FIELD claims that field. Each field's own `label` is
    an implicit alias — that way the canonical export's headers (which
    use the labels verbatim) always round-trip cleanly. A CSV header can
    map to at most one target — once claimed it's removed from the pool.
    """
    headers = list(headers)
    norm_to_orig = {_normalize_header(h): h for h in headers}
    used: set[str] = set()
    out: dict[str, str] = {}
    for tf in OUR_FIELDS:
        # The field's own label counts as an alias so the canonical export
        # round-trips even when the label has punctuation the alias list
        # didn't anticipate (e.g. "Type / format").
        for alias in (tf.label, *tf.aliases):
            norm = _normalize_header(alias)
            orig = norm_to_orig.get(norm)
            if orig and orig not in used:
                out[tf.key] = orig
                used.add(orig)
                break
    return out


# Type-translation table — applied at commit time when the mapped value
# comes from the chosen `format` column. CSV values are uppercase enums in
# the user's example data; map them onto our lowercase free-form strings.
_FORMAT_MAP = {
    "tpb": "trade paperback",
    "trade paperback": "trade paperback",
    "hc": "hardcover",
    "hardcover": "hardcover",
    "omnibus": "omnibus",
    "single_issue": "single issue",
    "single issue": "single issue",
    "issue": "single issue",
    "ogn": "graphic novel",
    "graphic novel": "graphic novel",
    "digital": "digital",
}


def translate_format(value: str | None) -> str | None:
    """Coerce a raw `Type` field into our internal `format` vocabulary.
    Unknown values are returned lowercased verbatim — better to keep
    user data than to silently drop it.

    Use this anywhere a user-supplied or upstream-supplied `format` value
    is about to be persisted on a Comic, so the DB stays in canonical
    lowercase form regardless of the casing the source happened to use.
    """
    if not value:
        return None
    norm = re.sub(r"\s+", " ", value).strip().lower()
    if not norm:
        return None
    return _FORMAT_MAP.get(norm, norm)


# ---------------------------------------------------------------------------
# Canonical header for the round-trippable CSV template + library export.
# Order matches the most natural reading order; columns line up 1:1 with
# OUR_FIELDS so the import autosuggester matches every header automatically.
# ---------------------------------------------------------------------------


def canonical_csv_headers() -> list[str]:
    """Header labels written into the empty template + library export.
    The autosuggest matcher in `suggest_mapping()` recognises each one,
    so re-uploading either file lands every column on the right field."""
    return [tf.label for tf in OUR_FIELDS]

