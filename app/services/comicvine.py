"""ComicVine metadata client.

ComicVine is used only for issue-ID lookups: hit `/issue/4000-<id>/` directly
(4000- is ComicVine's issue prefix). ISBNs aren't indexed in CV's search,
so the ISBN flow goes through Metron + Open Library instead.

Cached via the MetadataCache table so re-scans don't burn the 200/hr rate limit.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from app.config import settings
from app.services.cache import get_or_set
from app.services.errors import UpstreamRateLimit
from app.services.schemas import CreatorRef, LookupCandidate

BASE_URL = "https://comicvine.gamespot.com/api"
SOURCE = "comicvine"


def is_configured() -> bool:
    return bool(settings.comicvine_api_key)


def _headers() -> dict[str, str]:
    return {"User-Agent": settings.comicvine_user_agent, "Accept": "application/json"}


def _params(extra: dict[str, Any]) -> dict[str, Any]:
    return {"api_key": settings.comicvine_api_key, "format": "json", **extra}


def _candidate_from_issue(item: dict[str, Any]) -> LookupCandidate:
    image = item.get("image") or {}
    volume = item.get("volume") or {}
    publisher = (item.get("publisher") or {}).get("name") if item.get("publisher") else None

    creators: list[CreatorRef] = []
    # ComicVine returns each person once per role assignment; the role string
    # may be comma-separated for multi-role credits ("writer, artist").
    for person in (item.get("person_credits") or []):
        name = person.get("name")
        if not name:
            continue
        role = person.get("role") or None
        if role and "," in role:
            for r in role.split(","):
                r = r.strip()
                if r:
                    creators.append(CreatorRef(name=name, role=r))
        else:
            creators.append(CreatorRef(name=name, role=role))

    arcs: list[str] = []
    for arc in (item.get("story_arc_credits") or []):
        n = arc.get("name")
        if n:
            arcs.append(n)

    characters: list[str] = []
    for ch in (item.get("character_credits") or []):
        n = ch.get("name")
        if n:
            characters.append(n)

    concepts: list[str] = []
    for cn in (item.get("concept_credits") or []):
        n = cn.get("name")
        if n:
            concepts.append(n)

    return LookupCandidate(
        source=SOURCE,
        source_id=str(item.get("id")) if item.get("id") is not None else None,
        title=item.get("name"),
        series=volume.get("name"),
        issue_number=item.get("issue_number"),
        publisher=publisher,
        cover_date=item.get("cover_date") or item.get("store_date"),
        description=item.get("deck") or item.get("description"),
        cover_url=image.get("super_url") or image.get("medium_url") or image.get("original_url"),
        page_count=None,
        creators=creators,
        format=item.get("binding") or None,
        story_arcs=arcs,
        characters=characters,
        concepts=concepts,
        raw=item,
    )


async def _fetch(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> dict[str, Any]:
    r = await client.get(f"{BASE_URL}{path}", params=_params(params), headers=_headers())
    if r.status_code == 429:
        raise UpstreamRateLimit(SOURCE, "200/hr quota exhausted")
    r.raise_for_status()
    payload = r.json()
    # ComicVine signals soft errors with status_code != 1 in the JSON body —
    # status_code 107 is "Rate Limit Exceeded".
    if isinstance(payload, dict) and payload.get("status_code") == 107:
        raise UpstreamRateLimit(SOURCE, payload.get("error") or "rate-limited")
    return payload


async def get_issue(issue_id: str) -> Optional[LookupCandidate]:
    if not is_configured():
        return None

    async def fetch() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await _fetch(client, f"/issue/4000-{issue_id}/", {})

    payload = await get_or_set(source=SOURCE, key=f"issue:{issue_id}", fetch=fetch)
    item = payload.get("results")
    if not item:
        return None
    return _candidate_from_issue(item)


TEXT_SEARCH_LIMIT = 20


async def search_text(query: str) -> list[LookupCandidate]:
    """Free-text search across ComicVine issues. Hits the public
    `/search/?resources=issue&query=...` endpoint, capped at
    `TEXT_SEARCH_LIMIT` for speed.

    The search endpoint returns shallower issue payloads than
    `/issue/4000-<id>/` does — no `person_credits` / `story_arc_credits`
    / `binding`. The full record is fetched at /add/save time when the
    user picks one of these candidates (see `_refetch_candidate`).
    """
    if not is_configured():
        return []

    async def fetch() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await _fetch(client, "/search/", {
                "resources": "issue",
                "query": query,
                "limit": str(TEXT_SEARCH_LIMIT),
            })

    payload = await get_or_set(source=SOURCE, key=f"search:{query}", fetch=fetch)
    items = [it for it in (payload.get("results") or []) if isinstance(it, dict)]
    return [_candidate_from_issue(it) for it in items[:TEXT_SEARCH_LIMIT]]


async def get_volume_issues(volume_id: str) -> list[str]:
    """Pull the issue list for a ComicVine volume (their term for series).

    Returns a list of "Issue #N" labels (with the issue title appended when
    one exists), in the order ComicVine returns them — typically by issue
    number ascending. Storage shape matches the matcher in
    `services.series_progress`: trailing-digit fallback against
    `Comic.issue_number` is what makes ownership detection work.
    """
    if not is_configured():
        return []

    async def fetch() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await _fetch(
                client, f"/volume/4050-{volume_id}/", {"field_list": "issues,name"}
            )

    payload = await get_or_set(source=SOURCE, key=f"volume:{volume_id}", fetch=fetch)
    results = (payload or {}).get("results") or {}
    issues = results.get("issues") or []
    out: list[str] = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        num = it.get("issue_number")
        name = it.get("name")
        if not num and not name:
            continue
        label = f"#{num}" if num else "(no number)"
        if name:
            label = f"{label} — {name}"
        out.append(label)
    return out
