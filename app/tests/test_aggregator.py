"""Aggregator outcome handling: rate-limited vs unreachable sources."""

from __future__ import annotations

import asyncio

import httpx

from app.services import aggregator
from app.services.aggregator import LookupResult, _merge, _safe, search_text
from app.services.errors import UpstreamRateLimit
from app.services.schemas import LookupCandidate


def test_safe_flags_unreachable_on_generic_failure():
    async def _boom():
        raise httpx.ConnectTimeout("metron down")

    outcome = asyncio.run(_safe("metron", _boom()))
    assert outcome.unreachable is True
    assert outcome.rate_limited is False
    assert outcome.candidates == []


def test_safe_flags_rate_limited_distinctly():
    async def _throttled():
        raise UpstreamRateLimit("comicvine", "429")

    outcome = asyncio.run(_safe("comicvine", _throttled()))
    assert outcome.rate_limited is True
    assert outcome.unreachable is False


def test_merge_collects_both_warning_kinds():
    ok = aggregator._SourceOutcome(
        label="wookieepedia",
        candidates=[LookupCandidate(source="wookieepedia", title="X")],
    )
    down = aggregator._SourceOutcome(label="metron", unreachable=True)
    throttled = aggregator._SourceOutcome(label="comicvine", rate_limited=True)
    result = _merge(ok, down, throttled)
    assert len(result.candidates) == 1          # the responding source's hit survives
    assert result.unreachable == ["metron"]
    assert result.rate_limited == ["comicvine"]


def test_search_text_surfaces_unreachable_source(monkeypatch):
    """When one source's request blows up, search_text still returns the
    others' candidates AND names the failed source in `unreachable`."""
    async def _wookiee_ok(q):
        return [LookupCandidate(source="wookieepedia", title="Hit", series="S")]

    async def _metron_down(q):
        raise httpx.ConnectTimeout("metron unreachable")

    async def _cv_ok(q):
        return []

    monkeypatch.setattr(aggregator.wookieepedia, "search_text", _wookiee_ok)
    monkeypatch.setattr(aggregator.metron, "search_text", _metron_down)
    monkeypatch.setattr(aggregator.comicvine, "search_text", _cv_ok)

    result = asyncio.run(search_text("saga"))
    assert isinstance(result, LookupResult)
    assert "metron" in result.unreachable
    assert any(c.title == "Hit" for c in result.candidates)
