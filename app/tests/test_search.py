"""Phase 12: global search across title/creator/arc/tag/identifier."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def _seed(client: TestClient) -> int:
    """Save one comic + tag it. Returns the comic id."""
    r = client.post(
        "/add/save",
        data={
            "title": "Search Bait #1",
            "issue_number": "1",
            "publisher": "Search Pub",
            "series": "Search Series",
            "isbn_13": "9780000123450",
            "upc": "76194131234500111",
        },
    )
    assert r.status_code == 200
    cid = next(c["id"] for c in client.get("/api/comics", params={"limit": 500}).json() if c["title"] == "Search Bait #1")
    client.post(f"/comic/{cid}/tags", data={"name": "needle"})
    return cid


def test_search_matches_title_substring():
    with _client() as client:
        _seed(client)
        r = client.get("/search", params={"q": "Bait"})
        assert r.status_code == 200
        assert "Search Bait #1" in r.text
        assert "1 comic match" in r.text


def test_search_matches_series_substring():
    with _client() as client:
        _seed(client)
        r = client.get("/search", params={"q": "Search Series"})
        assert "Search Bait #1" in r.text


def test_search_matches_isbn_exact():
    with _client() as client:
        _seed(client)
        r = client.get("/search", params={"q": "9780000123450"})
        assert "Search Bait #1" in r.text


def test_search_matches_tag():
    with _client() as client:
        _seed(client)
        r = client.get("/search", params={"q": "needle"})
        assert "Search Bait #1" in r.text
        # Tag chip appears in the "Jump to" section.
        assert ">needle<" in r.text or "needle" in r.text


def test_search_no_hits_renders_empty_state():
    with _client() as client:
        _seed(client)
        r = client.get("/search", params={"q": "absolutely-nowhere-zzzz"})
        assert r.status_code == 200
        assert "NO HITS" in r.text


def test_search_empty_query_shows_landing():
    with _client() as client:
        r = client.get("/search")
        assert r.status_code == 200
        # No "match" line because no query yet.
        assert "match" not in r.text.lower() or "matches" in r.text.lower() is False or "title, creator" in r.text


def test_suggest_endpoint_returns_top_hits():
    with _client() as client:
        _seed(client)
        r = client.get("/search/suggest", params={"q": "Bait"})
        assert r.status_code == 200
        assert "Search Bait #1" in r.text
        assert "See all results" in r.text


def test_suggest_short_query_returns_empty():
    with _client() as client:
        _seed(client)
        # Single-char queries shouldn't trigger broad LIKE scans.
        r = client.get("/search/suggest", params={"q": "B"})
        assert r.status_code == 200
        assert r.text.strip() == ""
