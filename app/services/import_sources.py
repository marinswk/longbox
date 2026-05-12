"""Source-tile metadata for the CSV import wizard's step 3.

Centralizes:
  * The list of metadata sources the wizard knows about.
  * Each source's "is configured" probe (re-uses the per-service
    `is_configured()` helpers — no env-var sniffing here).
  * Smart-default selection based on what the user's mapped CSV looks like:
      - Wookieepedia auto-on if any row's `fandom` mentions "star wars".
      - ComicVine auto-on if its API key is set.
      - Metron auto-on if its credentials are set.
      - Open Library auto-on only if any row has ISBN or UPC mapped.

The result feeds the template directly so the source-picker renders with
sane checkboxes pre-ticked and a small status hint per tile.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from app.services import comicvine, metron, openlibrary, wookieepedia


@dataclass
class SourceTile:
    key: str
    name: str
    tagline: str
    configured: bool
    default_on: bool
    status: str   # one-line text shown under the tile name (e.g. "API key set")


def _has_any_isbn_or_upc(rows: Iterable[dict[str, str]], column_map: dict[str, str]) -> bool:
    isbn_col = column_map.get("isbn_13")
    upc_col = column_map.get("upc")
    if not (isbn_col or upc_col):
        return False
    for row in rows:
        if isbn_col and (row.get(isbn_col) or "").strip():
            return True
        if upc_col and (row.get(upc_col) or "").strip():
            return True
    return False


def _has_star_wars_fandom(rows: Iterable[dict[str, str]], column_map: dict[str, str]) -> bool:
    fcol = column_map.get("fandom")
    if not fcol:
        return False
    for row in rows:
        if "star wars" in (row.get(fcol) or "").strip().lower():
            return True
    return False


def build_source_tiles(
    rows: Iterable[dict[str, str]],
    column_map: dict[str, str],
    *,
    chosen_sources: list[str] | None = None,
) -> list[SourceTile]:
    """Compute the four source tiles. `chosen_sources`, when given, locks the
    `default_on` value to the user's previous selection (so revisiting the
    page doesn't overwrite their choice with a fresh smart default)."""
    rows = list(rows)
    sw_present = _has_star_wars_fandom(rows, column_map)
    isbn_present = _has_any_isbn_or_upc(rows, column_map)
    locked = set(chosen_sources or [])

    def _on(default: bool, key: str) -> bool:
        if chosen_sources is not None:
            return key in locked
        return default

    tiles: list[SourceTile] = []

    wp_ok = wookieepedia.is_configured()
    tiles.append(SourceTile(
        key="wookieepedia",
        name="Wookieepedia",
        tagline="Star Wars universe — best-in-class for SW comics, returns nothing for others.",
        configured=wp_ok,
        default_on=_on(wp_ok and sw_present, "wookieepedia"),
        status=("Auto-selected — your CSV mentions Star Wars." if sw_present
                else "Public MediaWiki API, no key required."),
    ))

    cv_ok = comicvine.is_configured()
    tiles.append(SourceTile(
        key="comicvine",
        name="ComicVine",
        tagline="Big general-purpose database — Marvel, DC, Image, indies, manga.",
        configured=cv_ok,
        default_on=_on(cv_ok, "comicvine"),
        status=("API key configured · 200 req/hr quota" if cv_ok
                else "API key missing — add COMICVINE_API_KEY to enable."),
    ))

    mt_ok = metron.is_configured()
    tiles.append(SourceTile(
        key="metron",
        name="Metron",
        tagline="Independent + DC focus, often has cleaner data than CV.",
        configured=mt_ok,
        default_on=_on(mt_ok, "metron"),
        status=("Credentials set — ready." if mt_ok
                else "Credentials missing — set METRON_USERNAME/PASSWORD."),
    ))

    ol_ok = openlibrary.is_configured()
    tiles.append(SourceTile(
        key="openlibrary",
        name="Open Library",
        tagline="ISBN-only matching for trade paperbacks/hardcovers.",
        configured=ol_ok,
        default_on=_on(ol_ok and isbn_present, "openlibrary"),
        status=("Auto-selected — your CSV has ISBN/UPC values." if isbn_present
                else "No key required. Will only match if you map ISBN-13 or UPC."),
    ))

    return tiles
