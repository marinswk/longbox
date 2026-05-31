"""Open Library ISBN client.

Keyless. Used as the fallback when ComicVine doesn't have the ISBN
(common for non-Marvel/DC trades) or isn't configured.

Endpoint: https://openlibrary.org/api/books?bibkeys=ISBN:<isbn>&format=json&jscmd=data
Cover:   https://covers.openlibrary.org/b/isbn/<isbn>-L.jpg
"""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from app.services.cache import get_or_set
from app.services.schemas import LookupCandidate

BASE_URL = "https://openlibrary.org"
COVERS_URL = "https://covers.openlibrary.org"
SOURCE = "openlibrary"


def is_configured() -> bool:
    return True


# A trailing volume marker on a trade title: the separator (space /
# colon / comma / dash) + a marker word + everything after it.
#   "Saga: Volume One"          -> strip ": Volume One"  -> "Saga"
#   "Locke & Key, Vol. 1"       -> strip ", Vol. 1"       -> "Locke & Key"
#   "The Sandman Vol. 1: …"     -> strip " Vol. 1: …"     -> "The Sandman"
# `Compendium` / `Omnibus` / `Deluxe` are deliberately NOT markers —
# those are distinct collected editions, not entries in the numbered
# run, so they keep their own series identity.
_VOLUME_MARKER = re.compile(
    r"[\s:,\-–—]+(?:vol\.?|volume|book|part)\b.*$",
    re.IGNORECASE,
)


def _derive_series(title: Optional[str]) -> Optional[str]:
    """Best-effort series name from a trade title when Open Library
    supplies none.

    Open Library has no real series field for most comic trades, so a
    scanned Saga / Locke & Key / Immortal Hulk collection would
    otherwise scatter into one self-named series per volume. Stripping
    a trailing volume marker recovers the shared series so the volumes
    group:

        "Saga, Volume Five"           -> "Saga"
        "Immortal Hulk Vol. 3"        -> "Immortal Hulk"
        "The Sandman Vol. 1: Preludes" -> "The Sandman"

    Returns ``None`` when no volume marker is present (e.g. a Sandman
    volume titled only by its story arc, "Dream Country") so the
    caller's existing title-as-series fallback still applies and the
    user can assign the series by hand."""
    if not title:
        return None
    stripped = _VOLUME_MARKER.sub("", title).strip(" :,-–—")
    if stripped and stripped != title.strip():
        return stripped
    return None


def _candidate_from_book(isbn: str, item: dict[str, Any]) -> LookupCandidate:
    publishers = item.get("publishers") or []
    authors = item.get("authors") or []

    # OL puts the volume/edition-specific name in `subtitle`. For comic
    # trades that's almost always the most identifying field, e.g.
    # title="Star Wars", subtitle="Jedi Knights Vol. 1 - Guardians …".
    # We compose them so the picker and the saved Comic show something useful.
    base_title = (item.get("title") or "").strip() or None
    subtitle = (item.get("subtitle") or "").strip() or None
    if base_title and subtitle:
        title = f"{base_title}: {subtitle}"
    else:
        title = base_title or subtitle

    # OL's cover-by-isbn endpoint returns a tiny placeholder image (HTTP 200,
    # ~43 bytes) when no real cover exists. ?default=false makes it 404 instead
    # so our cover downloader gracefully skips placeholders.
    cover = (item.get("cover") or {}).get("large")
    if not cover:
        cover = f"{COVERS_URL}/b/isbn/{isbn}-L.jpg?default=false"

    # Prefer OL's own series field when present (rare for trades);
    # otherwise derive one from the title so volumes group instead of
    # scattering into one series per book.
    series = (item.get("series") or [None])[0] if item.get("series") else None
    if not series:
        series = _derive_series(title)

    return LookupCandidate(
        source=SOURCE,
        source_id=item.get("key"),
        title=title,
        series=series,
        publisher=publishers[0]["name"] if publishers and isinstance(publishers[0], dict) else (publishers[0] if publishers else None),
        cover_date=item.get("publish_date"),
        description=(item.get("notes") if isinstance(item.get("notes"), str) else None),
        cover_url=cover,
        isbn_10=isbn if len(isbn) == 10 else None,
        isbn_13=isbn if len(isbn) == 13 else None,
        page_count=item.get("number_of_pages"),
        raw={"book": item, "authors": authors},
    )


async def search_isbn(isbn: str) -> list[LookupCandidate]:
    async def fetch() -> dict[str, Any]:
        params = {"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{BASE_URL}/api/books", params=params)
            r.raise_for_status()
            return r.json() or {}

    payload = await get_or_set(source=SOURCE, key=f"isbn:{isbn}", fetch=fetch)
    book = payload.get(f"ISBN:{isbn}")
    if not book:
        return []
    return [_candidate_from_book(isbn, book)]
