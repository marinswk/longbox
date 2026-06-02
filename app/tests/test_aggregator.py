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


def test_text_search_sources_includes_google_books():
    from app.services.aggregator import text_search_sources
    keys = {s["key"] for s in text_search_sources()}
    assert {"wookieepedia", "comicvine", "metron", "googlebooks"} <= keys


def test_isbn_lookup_merges_openlibrary_and_google_books(monkeypatch):
    """lookup_full for an ISBN combines the OL + Google Books records
    for that ISBN into one enriched candidate."""
    from app.services.schemas import CreatorRef, LookupCandidate

    isbn = "9781607066019"

    async def _ol(i):
        return [LookupCandidate(source="openlibrary", source_id="OL", title="Saga: Volume One",
                                isbn_13=isbn, cover_url="https://ol/cover.jpg",
                                creators=[CreatorRef(name="Fiona Staples", role="author")])]

    async def _gb(i):
        return [LookupCandidate(source="googlebooks", source_id="GB", title="Saga: Volume One",
                                isbn_13=isbn, description="rich desc", page_count=160,
                                creators=[CreatorRef(name="Brian K. Vaughan", role="author")])]

    async def _wk(i):
        return []

    monkeypatch.setattr(aggregator.openlibrary, "search_isbn", _ol)
    monkeypatch.setattr(aggregator.googlebooks, "search_isbn", _gb)
    monkeypatch.setattr(aggregator.wookieepedia, "search_isbn", _wk)

    res = asyncio.run(aggregator.lookup_full(isbn))
    assert len(res.candidates) == 1
    m = res.candidates[0]
    assert set(m.raw["merged_from"]) == {"openlibrary", "googlebooks"}
    assert m.description == "rich desc"
    assert m.cover_url == "https://ol/cover.jpg"
    assert {cr.name for cr in m.creators} == {"Brian K. Vaughan", "Fiona Staples"}
