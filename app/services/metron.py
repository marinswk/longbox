"""Metron metadata client.

Metron (https://metron.cloud) is a community-curated comic DB. We use it
for **issue-ID lookups only** (GET /api/issue/<metron_id>/).

Why no ISBN lookup: Metron's REST API has no public filter for ISBN. The
?isbn= query param is silently dropped by /api/issue/, /api/series/,
/api/arc/, etc. — every call returns the full default page regardless.
ISBNs are stored on the issue payload but unsearchable. /api/collection/
is the user's own personal library tracker (auth-scoped), not a public
ISBN index. ISBN lookups go through Open Library instead — see PLAN.md §3.

Auth is HTTP Basic (METRON_USER + METRON_PASS).
"""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from app.config import settings
from app.services.cache import get_or_set
from app.services.errors import UpstreamRateLimit
from app.services.schemas import CreatorRef, LookupCandidate

BASE_URL = "https://metron.cloud/api"
SOURCE = "metron"


def is_configured() -> bool:
    return bool(settings.metron_user and settings.metron_pass)


def _auth() -> httpx.BasicAuth:
    return httpx.BasicAuth(settings.metron_user or "", settings.metron_pass or "")


def _headers() -> dict[str, str]:
    return {"User-Agent": settings.comicvine_user_agent, "Accept": "application/json"}


def _candidate_from_issue(item: dict[str, Any]) -> LookupCandidate:
    series = item.get("series") or {}
    publisher = series.get("publisher")
    publisher_name = publisher.get("name") if isinstance(publisher, dict) else publisher
    image = item.get("image") or item.get("cover_url")
    issue_isbn = item.get("isbn")

    creators: list[CreatorRef] = []
    # Metron's `credits` shape: [{"creator": {"name": "..."}, "role": [{"name": "writer"}, ...]}]
    # Older payloads sometimes return role as a plain string.
    for entry in (item.get("credits") or []):
        creator_obj = entry.get("creator")
        name = creator_obj.get("name") if isinstance(creator_obj, dict) else creator_obj
        if not name:
            continue
        roles = entry.get("role") or [None]
        if not isinstance(roles, list):
            roles = [roles]
        for role in roles:
            role_name = role.get("name") if isinstance(role, dict) else role
            creators.append(CreatorRef(name=name, role=role_name))

    arcs: list[str] = []
    for arc in (item.get("arcs") or []):
        if isinstance(arc, dict):
            n = arc.get("name")
            if n:
                arcs.append(n)
        elif isinstance(arc, str):
            arcs.append(arc)

    characters: list[str] = []
    for ch in (item.get("characters") or []):
        if isinstance(ch, dict):
            n = ch.get("name")
            if n:
                characters.append(n)
        elif isinstance(ch, str):
            characters.append(ch)

    # Metron's series payload sometimes carries a `series_type` ("Single
    # Issue", "Trade Paperback", "Hard Cover") which maps cleanly to format.
    series_type = None
    if isinstance(series, dict):
        st = series.get("series_type")
        if isinstance(st, dict):
            series_type = st.get("name")
        elif isinstance(st, str):
            series_type = st

    upc = item.get("upc") or item.get("sku")

    return LookupCandidate(
        source=SOURCE,
        source_id=str(item.get("id")) if item.get("id") is not None else None,
        title=item.get("name") or item.get("title"),
        series=series.get("name") if isinstance(series, dict) else series,
        issue_number=item.get("number"),
        publisher=publisher_name,
        cover_date=item.get("cover_date"),
        description=item.get("desc") or item.get("description"),
        cover_url=image,
        isbn_13=issue_isbn if issue_isbn and len(str(issue_isbn)) == 13 else None,
        isbn_10=issue_isbn if issue_isbn and len(str(issue_isbn)) == 10 else None,
        upc=str(upc) if upc else None,
        page_count=item.get("page") or item.get("page_count"),
        creators=creators,
        format=series_type,
        story_arcs=arcs,
        characters=characters,
        raw=item,
    )


def _check_rate_limited(r: httpx.Response) -> None:
    if r.status_code == 429:
        raise UpstreamRateLimit(SOURCE, "throttled")


async def get_issue(metron_id: str) -> Optional[LookupCandidate]:
    if not is_configured():
        return None

    async def fetch() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0, auth=_auth(), headers=_headers()) as client:
            r = await client.get(f"{BASE_URL}/issue/{metron_id}/")
            _check_rate_limited(r)
            r.raise_for_status()
            return r.json()

    payload = await get_or_set(source=SOURCE, key=f"issue:{metron_id}", fetch=fetch)
    if not payload or not payload.get("id"):
        return None
    return _candidate_from_issue(payload)


TEXT_SEARCH_LIMIT = 20


def _shallow_candidate_from_list_item(item: dict[str, Any]) -> Optional[LookupCandidate]:
    """The list endpoint returns a stripped-down per-issue payload.
    Build a LookupCandidate from what's available; the user's pick will
    be hydrated through `get_issue` at /add/save time."""
    iid = item.get("id")
    if iid is None:
        return None

    # Metron's `issue` field is pre-formatted "Series Name (Year) #N".
    # Parse it into series + issue_number for the picker.
    raw_label = (item.get("issue") or "").strip()
    series_part, _, num_part = raw_label.rpartition(" #")
    if num_part:
        # Drop trailing "(YYYY)" from the series name if present.
        series_clean = re.sub(r"\s*\(\d{4}\)\s*$", "", series_part).strip() or None
        issue_number = num_part.strip() or None
    else:
        series_clean = raw_label or None
        issue_number = None

    cover = item.get("image") or item.get("cover_url")
    return LookupCandidate(
        source=SOURCE,
        source_id=str(iid),
        title=raw_label or None,
        series=series_clean,
        issue_number=issue_number,
        cover_date=item.get("cover_date"),
        cover_url=cover,
        raw=item,
    )


async def search_text(query: str) -> list[LookupCandidate]:
    """Free-text search via `/api/issue/?series_name=<q>`. Capped at
    `TEXT_SEARCH_LIMIT` to keep latency bounded. Returns shallow
    candidates that get hydrated via `get_issue` when the user picks one.
    """
    if not is_configured():
        return []

    async def fetch() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0, auth=_auth(), headers=_headers()) as client:
            r = await client.get(
                f"{BASE_URL}/issue/",
                params={"series_name": query, "page": 1},
            )
            _check_rate_limited(r)
            r.raise_for_status()
            return r.json()

    payload = await get_or_set(source=SOURCE, key=f"search-text:{query}", fetch=fetch)
    out: list[LookupCandidate] = []
    for item in (payload.get("results") or [])[:TEXT_SEARCH_LIMIT]:
        if isinstance(item, dict):
            cand = _shallow_candidate_from_list_item(item)
            if cand is not None:
                out.append(cand)
    return out


async def search_upc(upc: str) -> list[LookupCandidate]:
    """Look up a comic by UPC barcode.

    Metron exposes a real `?upc=` filter on `/api/issue/?upc=<upc>` —
    most other sources (ComicVine, Open Library) don't. Coverage is
    spotty for older / non-US comics, but this is the best public option
    we've got beyond Wookieepedia full-text matching.

    Tries the full UPC first (which can be 17–18 digits with a variant
    suffix), then falls back to the 12-digit UPC-A prefix, since some
    Metron records store the bare UPC-A without the variant tail.
    """
    if not is_configured():
        return []

    candidates: list[LookupCandidate] = []
    seen_ids: set[str] = set()

    # Try full UPC, then the 12-digit prefix. Stop early if we get hits.
    queries = [upc]
    if len(upc) > 12:
        queries.append(upc[:12])

    for q in queries:
        async def fetch(q=q) -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=10.0, auth=_auth(), headers=_headers()) as client:
                r = await client.get(f"{BASE_URL}/issue/", params={"upc": q})
                _check_rate_limited(r)
                r.raise_for_status()
                return r.json()

        payload = await get_or_set(source=SOURCE, key=f"upc:{q}", fetch=fetch)
        for item in payload.get("results") or []:
            issue_id = str(item.get("id") or "")
            if not issue_id or issue_id in seen_ids:
                continue
            # The list endpoint doesn't include the rich per-issue
            # payload (creators, arcs, full image set). Re-fetch the
            # detail view via the existing get_issue path.
            detailed = await get_issue(issue_id)
            if detailed is not None:
                candidates.append(detailed)
                seen_ids.add(issue_id)

        if candidates:
            break  # full UPC matched — don't waste a call on the prefix

    return candidates


async def get_series_issues(series_id: str) -> list[str]:
    """Pull every issue belonging to a Metron series, paging through
    `/api/issue/?series=<id>` until exhausted.

    Returns labels of the form `#<number> — <title>` (title omitted when
    Metron didn't supply one). Storage shape matches the matcher:
    trailing-digit fallback against `Comic.issue_number` does the heavy
    lifting for ownership detection.
    """
    if not is_configured():
        return []

    async def fetch() -> dict[str, Any]:
        out_pages: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=10.0, auth=_auth(), headers=_headers()) as client:
            url = f"{BASE_URL}/issue/?series={series_id}&page=1"
            # Metron paginates with `next: <url>` in the body.
            while url:
                r = await client.get(url)
                _check_rate_limited(r)
                r.raise_for_status()
                page = r.json()
                out_pages.append(page)
                url = page.get("next")
        # Combine all pages' results into a single dict so we cache one blob.
        combined_results: list[Any] = []
        for p in out_pages:
            combined_results.extend(p.get("results") or [])
        return {"results": combined_results}

    payload = await get_or_set(source=SOURCE, key=f"series-issues:{series_id}", fetch=fetch)
    out: list[str] = []
    for it in payload.get("results") or []:
        if not isinstance(it, dict):
            continue
        num = it.get("number") or it.get("issue") or it.get("issue_number")
        # Metron's list endpoint exposes a per-issue `issue` text field that
        # already reads "Series Name #N" — but we only want the right-side
        # label so the matcher's trailing-digit fallback still works.
        name = it.get("name") or it.get("title")
        if not num and not name:
            continue
        label = f"#{num}" if num else "(no number)"
        if name:
            label = f"{label} — {name}"
        out.append(label)
    return out
