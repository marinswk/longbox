"""Library-wide cleanup job + its /library/cleanup endpoints.

Covers the orchestrator's start guard, the HTMX status fragment, and
a full end-to-end run that re-refreshes a Wookieepedia series.
"""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Series
from app.services import library_cleanup as lc


def _client() -> TestClient:
    return TestClient(create_app())


# A series article whose ==Issues== list we can refresh against.
_CLEANUP_SERIES_WIKITEXT = (
    "{{Top|rwm}}\n"
    "{{ComicSeries|title=''BE Cleanup Probe''}}\n"
    "==Issues==\n"
    "*[[BE Cleanup Probe 1]]\n"
    "*[[BE Cleanup Probe 2]]\n"
    "*[[BE Cleanup Probe 3]]\n"
    "==External links==\n"
)


def _route(request: httpx.Request) -> httpx.Response:
    qs = parse_qs(urlparse(str(request.url)).query)
    if qs.get("action", [None])[0] == "parse":
        title = qs.get("page", [""])[0]
        if title == "BE Cleanup Probe Series":
            return httpx.Response(200, json={"parse": {
                "title": title, "wikitext": {"*": _CLEANUP_SERIES_WIKITEXT},
            }})
    # Everything else (other accumulated test series/comics) 404s —
    # the job treats that as a skip, never a crash.
    return httpx.Response(404)


def _reset_progress() -> None:
    lc._progress = lc.CleanupProgress()
    lc._task = None


# ---------------------------------------------------------------------------
# Start guard
# ---------------------------------------------------------------------------


def test_start_cleanup_rejects_a_second_concurrent_run():
    """Only one cleanup runs at a time — a second start is a no-op."""
    async def _go() -> bool:
        lc._progress = lc.CleanupProgress(running=True)
        try:
            return await lc.start_cleanup()
        finally:
            _reset_progress()

    assert asyncio.run(_go()) is False


# ---------------------------------------------------------------------------
# Status fragment
# ---------------------------------------------------------------------------


def test_cleanup_status_endpoint_renders_progress_slot():
    _reset_progress()
    with _client() as client:
        r = client.get("/library/cleanup/status")
        assert r.status_code == 200
        assert 'id="cleanup-progress"' in r.text


# ---------------------------------------------------------------------------
# Full end-to-end run
# ---------------------------------------------------------------------------


@respx.mock
def test_cleanup_run_refreshes_a_wookieepedia_series():
    """A series carrying a Wookieepedia source gets its expected-issue
    list re-pulled, and the job ends in a non-running 'Finished'
    state with the series counted as refreshed."""
    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    _reset_progress()

    with _client():
        # Seed a series pointing at our mock article.
        async def _seed() -> int:
            async with SessionLocal() as session:
                s = Series(
                    name="BE Cleanup Probe Series",
                    source="wookieepedia",
                    source_id="BE Cleanup Probe Series",
                )
                session.add(s)
                await session.commit()
                await session.refresh(s)
                return s.id

        sid = asyncio.run(_seed())

        async def _run_job() -> None:
            started = await lc.start_cleanup()
            assert started is True
            await lc._task  # wait for the background task to finish

        asyncio.run(_run_job())

        p = lc.get_progress()
        assert p.running is False
        assert p.phase == "Finished"
        assert p.series_refreshed >= 1

        async def _read() -> str:
            async with SessionLocal() as session:
                s = (await session.exec(
                    select(Series).where(Series.id == sid)
                )).first()
                return s.expected_issues or ""

        expected = asyncio.run(_read())
        assert "BE Cleanup Probe 1" in expected
        assert "BE Cleanup Probe 3" in expected

    _reset_progress()


def test_cleanup_post_returns_running_fragment_without_double_starting():
    """When a run is already in flight, POSTing the button again just
    re-renders the live progress fragment (with the HTMX poll armed)
    instead of starting a second job."""
    _reset_progress()
    lc._progress = lc.CleanupProgress(
        running=True, phase="Refreshing series from upstream",
        phase_index=1, started_at=1.0, total=10, done=3,
    )
    try:
        with _client() as client:
            r = client.post("/library/cleanup")
            assert r.status_code == 200
            assert 'id="cleanup-progress"' in r.text
            # Poll trigger is armed while running.
            assert "/library/cleanup/status" in r.text
    finally:
        _reset_progress()
