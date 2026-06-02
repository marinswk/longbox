"""Google Books source: parsing, key gating, rate-limit handling."""

from __future__ import annotations

import asyncio

import httpx
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import app_settings, googlebooks

_VOLUME = {
    "items": [{
        "id": "GB123",
        "volumeInfo": {
            "title": "Saga",
            "subtitle": "Volume One",
            "authors": ["Brian K. Vaughan", "Fiona Staples"],
            "publisher": "Image Comics",
            "publishedDate": "2012-10-23",
            "description": "<p>When two soldiers from opposite sides…</p>",
            "pageCount": 160,
            "language": "en",
            "industryIdentifiers": [
                {"type": "ISBN_13", "identifier": "9781607066019"},
                {"type": "ISBN_10", "identifier": "1607066017"},
            ],
            "imageLinks": {"thumbnail": "http://books.google.com/books/content?id=GB123&zoom=1&edge=curl&source=gbs_api"},
        },
    }]
}


def _with_key(monkeypatch, key="gb-test-key"):
    monkeypatch.setattr(app_settings.CREDS, "google_books_api_key", key)


def test_not_configured_without_key(monkeypatch):
    monkeypatch.setattr(app_settings.CREDS, "google_books_api_key", None)
    assert googlebooks.is_configured() is False
    # search short-circuits to [] with no HTTP call.
    assert asyncio.run(googlebooks.search_isbn("9781607066019")) == []


def test_search_isbn_parses_volume(monkeypatch):
    with TestClient(create_app()):  # metadatacache table for the cache layer
        # Set the key AFTER the lifespan hydrate (which would otherwise
        # reset CREDS from the empty DB / env).
        monkeypatch.setattr(app_settings.CREDS, "google_books_api_key", "gb-test-key")
        assert googlebooks.is_configured() is True
        with respx.mock:
            route = respx.get("https://www.googleapis.com/books/v1/volumes").mock(
                return_value=httpx.Response(200, json=_VOLUME)
            )
            [c] = asyncio.run(googlebooks.search_isbn("9781607066019"))
    assert c.source == "googlebooks"
    assert c.title == "Saga: Volume One"
    assert c.series == "Saga"                     # derived from title
    assert c.publisher == "Image Comics"
    assert c.cover_date == "2012-10-23"
    assert c.page_count == 160
    assert c.isbn_13 == "9781607066019"
    assert [cr.name for cr in c.creators] == ["Brian K. Vaughan", "Fiona Staples"]
    # Description HTML stripped.
    assert "<p>" not in (c.description or "")
    # Cover: https-upgraded, edge-curl removed.
    assert c.cover_url.startswith("https://")
    assert "edge=curl" not in c.cover_url
    # The API key is sent on the request.
    sent = route.calls.last.request.url
    assert "key=gb-test-key" in str(sent)


def test_search_isbn_raises_rate_limit_on_429(monkeypatch):
    import pytest
    from app.services.errors import UpstreamRateLimit
    with TestClient(create_app()):
        monkeypatch.setattr(app_settings.CREDS, "google_books_api_key", "gb-test-key")
        with respx.mock:
            respx.get("https://www.googleapis.com/books/v1/volumes").mock(
                return_value=httpx.Response(429, json={})
            )
            # A distinct ISBN so no other test's cached payload satisfies this.
            with pytest.raises(UpstreamRateLimit):
                asyncio.run(googlebooks.search_isbn("9780000000429"))
