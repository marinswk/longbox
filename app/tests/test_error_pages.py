"""Themed 404 / 500 pages for full-page HTML routes.

JSON / API / HTMX requests still get the FastAPI default machine-readable
body so callers and HTMX swaps don't see HTML where they expect JSON.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_unknown_route_returns_themed_404_html():
    with _client() as client:
        r = client.get("/no-such-page", headers={"accept": "text/html"})
        assert r.status_code == 404
        assert "Lost in hyperspace" in r.text
        # The themed page extends _base.html → the nav is rendered.
        assert 'href="/library"' in r.text


def test_unknown_route_via_api_returns_json():
    with _client() as client:
        r = client.get("/api/comics/9999999")
        assert r.status_code == 404
        # Default FastAPI JSON shape.
        assert r.headers["content-type"].startswith("application/json")


def test_htmx_request_does_not_get_themed_html():
    with _client() as client:
        r = client.get("/no-such-page", headers={"HX-Request": "true", "accept": "text/html"})
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")


def test_unknown_comic_id_returns_themed_404():
    with _client() as client:
        r = client.get("/comic/99999999", headers={"accept": "text/html"})
        assert r.status_code == 404
        assert "Lost in hyperspace" in r.text
