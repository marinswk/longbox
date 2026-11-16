"""Fandom's 429 handling: the cool-down, the inference backfill stopping on
it, and the network backfills running in the background after start-up.

Without these, a throttled Wookieepedia turned every cold start into a
15-minute loop over the library: each title retried and probed, every
request a fresh connection and DNS lookup, the health check failing until
the backfill chain finished.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import settings
from app.db import SessionLocal
from app.main import create_app
from app.models import Comic
from app.services import fandoms, wookieepedia
from app.services.errors import UpstreamRateLimit


def _client() -> TestClient:
    return TestClient(create_app())


async def _call_api() -> dict:
    async with httpx.AsyncClient(timeout=5.0) as client:
        return await wookieepedia._api(client, {"action": "query", "list": "search", "srsearch": "x"})


@respx.mock
def test_429_starts_cool_down_and_later_calls_send_nothing():
    route = respx.get(wookieepedia.API_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "120"})
    )
    with pytest.raises(UpstreamRateLimit):
        asyncio.run(_call_api())
    assert wookieepedia.is_throttled()
    # Inside the cool-down: fails fast, no request (and so no DNS lookup).
    with pytest.raises(UpstreamRateLimit):
        asyncio.run(_call_api())
    assert route.call_count == 1
    remaining = wookieepedia._throttled_until - time.monotonic()
    assert 100 < remaining <= 120


@respx.mock
def test_retry_after_as_a_date_falls_back_to_the_default():
    respx.get(wookieepedia.API_URL).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    )
    with pytest.raises(UpstreamRateLimit):
        asyncio.run(_call_api())
    remaining = wookieepedia._throttled_until - time.monotonic()
    assert 0 < remaining <= wookieepedia.THROTTLE_DEFAULT_SECONDS


@respx.mock
def test_requests_resume_after_the_cool_down():
    route = respx.get(wookieepedia.API_URL).mock(
        return_value=httpx.Response(200, json={"query": {"search": []}})
    )
    wookieepedia._note_throttle("1")
    wookieepedia._throttled_until = time.monotonic() - 0.01  # cool-down over
    assert not wookieepedia.is_throttled()
    asyncio.run(_call_api())
    assert route.call_count == 1


def test_inference_backfill_stops_while_throttled(monkeypatch):
    with _client():
        pass  # migrations

    async def _seed():
        async with SessionLocal() as s:
            s.add(Comic(title="Throttle Omnibus", collected_issues="Throttle Test 1\nThrottle Test 2"))
            await s.commit()
    asyncio.run(_seed())

    calls: list[int] = []

    async def _fake_attach(cid: int) -> None:
        calls.append(cid)

    import app.routers.add as add_router
    monkeypatch.setattr(add_router, "_attach_inferred_series", _fake_attach)

    wookieepedia._note_throttle("300")
    assert asyncio.run(fandoms.backfill_inferred_series_from_collected_issues()) == 0
    assert calls == []

    wookieepedia.reset_throttle()
    asyncio.run(fandoms.backfill_inferred_series_from_collected_issues())
    assert calls  # without the cool-down it walks the comics again


def test_network_backfills_run_in_the_background(monkeypatch):
    """The app answers while the Wookieepedia backfill still runs, and the
    task is cancelled cleanly on shutdown."""
    monkeypatch.setattr(settings, "startup_backfills_in_background", True)
    cancelled: list[bool] = []

    async def _slow_backfill() -> int:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
        return 0

    monkeypatch.setattr(fandoms, "backfill_inferred_series_from_collected_issues", _slow_backfill)
    with _client() as client:
        r = client.get("/health")
        assert r.status_code == 200
    assert cancelled == [True]
