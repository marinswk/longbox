"""Open Library ISBN client.

Keyless. Used as the fallback when ComicVine doesn't have the ISBN
(common for non-Marvel/DC trades) or isn't configured.

Endpoint: https://openlibrary.org/api/books?bibkeys=ISBN:<isbn>&format=json&jscmd=data
Cover:   https://covers.openlibrary.org/b/isbn/<isbn>-L.jpg
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from app.services.cache import get_or_set
from app.services.schemas import LookupCandidate

BASE_URL = "https://openlibrary.org"
COVERS_URL = "https://covers.openlibrary.org"
SOURCE = "openlibrary"


def is_configured() -> bool:
    return True


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

    return LookupCandidate(
        source=SOURCE,
        source_id=item.get("key"),
        title=title,
        series=(item.get("series") or [None])[0] if item.get("series") else None,
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
