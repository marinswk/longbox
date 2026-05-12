"""ComicVine + Metron series-issue fetchers + rate-limit handling.

Covers:
  * comicvine.get_volume_issues parses /volume/4050-N/ → labels
  * metron.get_series_issues paginates /api/issue/?series=N → labels
  * 429 responses raise UpstreamRateLimit
  * /series/{id}/refresh accepts comicvine + metron sources
  * /add/lookup surfaces rate-limited sources to the picker context
"""

from __future__ import annotations

import asyncio

import httpx
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import comicvine, metron, wookieepedia
from app.services.errors import UpstreamRateLimit


def _client() -> TestClient:
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# ComicVine: get_volume_issues + 429 handling
# ---------------------------------------------------------------------------


@respx.mock
def test_comicvine_get_volume_issues_returns_labels():
    respx.get("https://comicvine.gamespot.com/api/volume/4050-42537/").mock(
        return_value=httpx.Response(200, json={
            "results": {
                "name": "Saga",
                "issues": [
                    {"id": 1, "issue_number": "1", "name": "Pilot"},
                    {"id": 2, "issue_number": "2", "name": "Children"},
                    {"id": 3, "issue_number": "3", "name": None},
                ],
            },
        })
    )
    with _client():  # ensure DB tables exist for the cache layer
        pass
    issues = asyncio.run(comicvine.get_volume_issues("42537"))
    assert issues == ["#1 — Pilot", "#2 — Children", "#3"]


@respx.mock
def test_comicvine_429_raises_rate_limit():
    respx.get("https://comicvine.gamespot.com/api/volume/4050-99/").mock(
        return_value=httpx.Response(429, json={"error": "OK"})
    )
    with _client():
        pass
    try:
        asyncio.run(comicvine.get_volume_issues("99"))
    except UpstreamRateLimit as exc:
        assert exc.source == "comicvine"
    else:
        raise AssertionError("expected UpstreamRateLimit")


@respx.mock
def test_comicvine_status_code_107_in_body_raises_rate_limit():
    """ComicVine sometimes returns 200 with `status_code: 107` to signal
    rate limiting. The client should still treat it as a rate limit."""
    respx.get("https://comicvine.gamespot.com/api/volume/4050-77/").mock(
        return_value=httpx.Response(200, json={
            "status_code": 107,
            "error": "Rate Limit Exceeded",
        })
    )
    with _client():
        pass
    try:
        asyncio.run(comicvine.get_volume_issues("77"))
    except UpstreamRateLimit as exc:
        assert exc.source == "comicvine"
        assert "rate limit" in exc.detail.lower()
    else:
        raise AssertionError("expected UpstreamRateLimit")


# ---------------------------------------------------------------------------
# Metron: get_series_issues (paginated) + 429 handling
# ---------------------------------------------------------------------------


@respx.mock
def test_metron_get_series_issues_paginates():
    page1 = {
        "next": "https://metron.cloud/api/issue/?series=10&page=2",
        "results": [
            {"id": 1, "number": "1", "name": "First"},
            {"id": 2, "number": "2", "name": "Second"},
        ],
    }
    page2 = {
        "next": None,
        "results": [
            {"id": 3, "number": "3", "name": None},
        ],
    }
    respx.get("https://metron.cloud/api/issue/?series=10&page=1").mock(
        return_value=httpx.Response(200, json=page1)
    )
    respx.get("https://metron.cloud/api/issue/?series=10&page=2").mock(
        return_value=httpx.Response(200, json=page2)
    )
    with _client():
        pass
    issues = asyncio.run(metron.get_series_issues("10"))
    assert issues == ["#1 — First", "#2 — Second", "#3"]


@respx.mock
def test_metron_429_raises_rate_limit():
    respx.get("https://metron.cloud/api/issue/?series=66&page=1").mock(
        return_value=httpx.Response(429, json={"detail": "throttled"})
    )
    with _client():
        pass
    try:
        asyncio.run(metron.get_series_issues("66"))
    except UpstreamRateLimit as exc:
        assert exc.source == "metron"
    else:
        raise AssertionError("expected UpstreamRateLimit")


# ---------------------------------------------------------------------------
# /series/{id}/refresh dispatches by source
# ---------------------------------------------------------------------------


@respx.mock
def test_series_refresh_dispatches_to_comicvine():
    respx.get("https://comicvine.gamespot.com/api/volume/4050-555/").mock(
        return_value=httpx.Response(200, json={
            "results": {"name": "Test", "issues": [
                {"issue_number": "1", "name": "A"},
                {"issue_number": "2", "name": "B"},
            ]},
        })
    )
    with _client() as client:
        # Save a comic so a Series row exists to refresh.
        client.post(
            "/add/save",
            data={"title": "X #1", "issue_number": "1", "publisher": "P",
                  "series": "CV Test Series", "isbn_13": "9786000000001"},
        )
        from app.db import SessionLocal
        from app.models import Series
        from sqlmodel import select

        async def _sid():
            async with SessionLocal() as s:
                row = (await s.exec(select(Series).where(Series.name == "CV Test Series"))).first()
                return row.id

        sid = asyncio.run(_sid())
        r = client.post(
            f"/series/{sid}/refresh",
            data={"source": "comicvine", "source_id": "555"},
        )
        assert r.status_code == 204
        page = client.get(f"/series/{sid}").text
        assert "#1" in page
        assert "#2" in page
        # The owned single issue (#1) should be matched via the trailing-digit
        # fallback since CV labels are "#N — Name".
        assert "Owned <span class=\"text-crawl-dark\">1</span> / 2" in page


def test_series_refresh_rejects_unknown_source():
    with _client() as client:
        client.post(
            "/add/save",
            data={"title": "Y #1", "issue_number": "1", "publisher": "P",
                  "series": "Unknown Src Series", "isbn_13": "9786000000002"},
        )
        from app.db import SessionLocal
        from app.models import Series
        from sqlmodel import select

        async def _sid():
            async with SessionLocal() as s:
                row = (await s.exec(select(Series).where(Series.name == "Unknown Src Series"))).first()
                return row.id

        sid = asyncio.run(_sid())
        r = client.post(
            f"/series/{sid}/refresh",
            data={"source": "marvel", "source_id": "1"},
        )
        assert r.status_code == 400
        assert "unsupported" in r.text


@respx.mock
def test_series_refresh_returns_429_when_upstream_throttled():
    respx.get("https://comicvine.gamespot.com/api/volume/4050-7/").mock(
        return_value=httpx.Response(429, json={})
    )
    with _client() as client:
        client.post(
            "/add/save",
            data={"title": "Z #1", "issue_number": "1", "publisher": "P",
                  "series": "Throttled Series", "isbn_13": "9786000000003"},
        )
        from app.db import SessionLocal
        from app.models import Series
        from sqlmodel import select

        async def _sid():
            async with SessionLocal() as s:
                row = (await s.exec(select(Series).where(Series.name == "Throttled Series"))).first()
                return row.id

        sid = asyncio.run(_sid())
        r = client.post(
            f"/series/{sid}/refresh",
            data={"source": "comicvine", "source_id": "7"},
        )
        assert r.status_code == 429
        assert "rate-limited" in r.text


# ---------------------------------------------------------------------------
# /add/lookup picker shows rate-limit warning chip
# ---------------------------------------------------------------------------


@respx.mock
def test_picker_shows_rate_limit_warning():
    """ComicVine returns 429 during an issue-id lookup → picker template
    surfaces a `comicvine` chip in the warning banner."""
    respx.get("https://comicvine.gamespot.com/api/issue/4000-9999/").mock(
        return_value=httpx.Response(429, json={})
    )
    respx.get("https://metron.cloud/api/issue/9999/").mock(
        return_value=httpx.Response(404)
    )
    with _client() as client:
        r = client.post("/add/lookup", data={"identifier": "9999"})
        assert r.status_code == 200
        assert "Some sources rate-limited" in r.text
        assert "comicvine" in r.text
