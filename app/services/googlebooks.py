"""Google Books metadata client.

Google Books' Volumes API has excellent coverage of trade paperbacks and
graphic novels, with far richer records than Open Library — full author
list, publisher, exact publish date, and a real description (often
1,000+ chars where OL has none).

Endpoint: https://www.googleapis.com/books/v1/volumes?q=isbn:<isbn>

The API works keyless but the shared per-IP quota is tiny and 429s
constantly, so we gate the source on an API key (set on the Admin →
Sources page, like ComicVine). The Volumes API is free — a key only
raises the quota, no billing.

Also exposes free-text search (`search_text`) since Google Books has a
strong general search that helps non-Star-Wars title lookups.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from app.services.app_settings import CREDS
from app.services.cache import get_or_set
from app.services.errors import UpstreamRateLimit
from app.services.schemas import CreatorRef, LookupCandidate
from app.services.series_naming import derive_series

API_URL = "https://www.googleapis.com/books/v1/volumes"
SOURCE = "googlebooks"
TEXT_SEARCH_LIMIT = 20

# Strip the HTML Google Books embeds in `description` (<p>, <br>, <i>…).
_HTML_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]*\n[ \t]*")


def is_configured() -> bool:
    # Gated on a key — keyless Google Books is too rate-limited to rely
    # on. Reads the runtime cache so a key saved on /admin takes effect
    # without a restart.
    return bool(CREDS.google_books_api_key)


def _params(extra: dict[str, Any]) -> dict[str, Any]:
    # `country` is required by the Volumes API for some queries; US is a
    # safe default for English-language comic trades.
    params: dict[str, Any] = {"country": "US", **extra}
    if CREDS.google_books_api_key:
        params["key"] = CREDS.google_books_api_key
    return params


def _clean_description(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    text = _HTML_TAG.sub("", raw)
    text = _WS.sub("\n", text).strip()
    return text or None


def _cover_url(volume_info: dict[str, Any]) -> Optional[str]:
    links = volume_info.get("imageLinks") or {}
    url = links.get("thumbnail") or links.get("smallThumbnail")
    if not url:
        return None
    # Google serves these over http with a page-curl effect; upgrade to
    # https and drop the curl for a clean front cover.
    url = url.replace("http://", "https://", 1).replace("&edge=curl", "")
    return url


def _candidate_from_volume(item: dict[str, Any]) -> Optional[LookupCandidate]:
    vi = item.get("volumeInfo") or {}
    base_title = (vi.get("title") or "").strip() or None
    subtitle = (vi.get("subtitle") or "").strip() or None
    title = f"{base_title}: {subtitle}" if base_title and subtitle else (base_title or subtitle)
    if not title:
        return None

    isbn_10 = isbn_13 = None
    for ident in (vi.get("industryIdentifiers") or []):
        t, v = ident.get("type"), ident.get("identifier")
        if t == "ISBN_13":
            isbn_13 = v
        elif t == "ISBN_10":
            isbn_10 = v

    creators = [CreatorRef(name=a, role="author") for a in (vi.get("authors") or []) if a]

    pages = vi.get("pageCount")
    page_count = pages if isinstance(pages, int) and pages > 0 else None

    return LookupCandidate(
        source=SOURCE,
        source_id=item.get("id"),
        title=title,
        series=derive_series(title),
        publisher=(vi.get("publisher") or "").strip() or None,
        cover_date=vi.get("publishedDate"),
        description=_clean_description(vi.get("description")),
        cover_url=_cover_url(vi),
        isbn_10=isbn_10,
        isbn_13=isbn_13,
        page_count=page_count,
        language=vi.get("language"),
        creators=creators,
        raw={"volume": item},
    )


def _check_rate_limited(r: httpx.Response) -> None:
    if r.status_code == 429:
        raise UpstreamRateLimit(SOURCE, "google books quota exceeded")


async def search_isbn(isbn: str) -> list[LookupCandidate]:
    if not is_configured():
        return []

    async def fetch() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(API_URL, params=_params({"q": f"isbn:{isbn}"}))
            _check_rate_limited(r)
            r.raise_for_status()
            return r.json() or {}

    payload = await get_or_set(source=SOURCE, key=f"isbn:{isbn}", fetch=fetch)
    items = [it for it in (payload.get("items") or []) if isinstance(it, dict)]
    # Prefer the edition whose identifiers include the queried ISBN and
    # that actually carries a cover; else take the first parseable item.
    out: list[LookupCandidate] = []
    for it in items[:3]:
        cand = _candidate_from_volume(it)
        if cand is not None:
            out.append(cand)
    # Return just the best single edition — multiple Google editions of
    # the same ISBN are noise in the picker.
    if not out:
        return []
    out.sort(key=lambda c: (c.cover_url is None, c.page_count is None))
    return out[:1]


async def search_text(query: str) -> list[LookupCandidate]:
    if not is_configured():
        return []

    async def fetch() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(API_URL, params=_params({
                "q": query, "maxResults": TEXT_SEARCH_LIMIT,
            }))
            _check_rate_limited(r)
            r.raise_for_status()
            return r.json() or {}

    payload = await get_or_set(source=SOURCE, key=f"text:{query}", fetch=fetch)
    out: list[LookupCandidate] = []
    for it in (payload.get("items") or [])[:TEXT_SEARCH_LIMIT]:
        if isinstance(it, dict):
            cand = _candidate_from_volume(it)
            if cand is not None:
                out.append(cand)
    return out
