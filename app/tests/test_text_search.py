"""Free-text search across Wookieepedia + ComicVine + Metron + pagination."""

from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import comicvine, metron, wookieepedia
from app.services.aggregator import search_text


def _client() -> TestClient:
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Per-source search_text
# ---------------------------------------------------------------------------


CV_SEARCH_PAYLOAD = {
    "results": [
        {"id": 11, "name": "Pilot", "issue_number": "1",
         "volume": {"name": "Saga"}, "image": {"super_url": "https://cv/1.jpg"},
         "publisher": {"name": "Image Comics"}, "person_credits": [],
         "story_arc_credits": []},
        {"id": 12, "name": None, "issue_number": "2",
         "volume": {"name": "Saga"}, "image": {"super_url": "https://cv/2.jpg"},
         "publisher": {"name": "Image Comics"}, "person_credits": []},
    ],
}


@respx.mock
def test_comicvine_search_text_returns_candidates():
    respx.get("https://comicvine.gamespot.com/api/search/").mock(
        return_value=httpx.Response(200, json=CV_SEARCH_PAYLOAD)
    )
    with _client():
        pass
    out = asyncio.run(comicvine.search_text("txtsearch-zzx"))
    assert len(out) == 2
    assert out[0].source == "comicvine"
    assert out[0].series == "Saga"
    assert out[0].issue_number == "1"


METRON_LIST_PAYLOAD = {
    "count": 2,
    "next": None,
    "results": [
        {"id": 100, "issue": "Saga (2012) #1", "cover_date": "2012-03-14",
         "image": "https://m/1.jpg"},
        {"id": 101, "issue": "Saga (2012) #2", "cover_date": "2012-04-15",
         "image": "https://m/2.jpg"},
    ],
}


@respx.mock
def test_metron_search_text_parses_series_and_number():
    respx.get("https://metron.cloud/api/issue/").mock(
        return_value=httpx.Response(200, json=METRON_LIST_PAYLOAD)
    )
    with _client():
        pass
    out = asyncio.run(metron.search_text("txtsearch-zzx"))
    assert len(out) == 2
    assert out[0].source == "metron"
    assert out[0].series == "Saga"  # "(2012)" stripped from series name
    assert out[0].issue_number == "1"
    assert out[0].source_id == "100"


WOOKIEE_SEARCH_PAYLOAD = {
    "query": {"search": [{"title": "Star Wars: TextSearch Saga", "pageid": 1}]},
}
WOOKIEE_PARSE_PAYLOAD = {
    "parse": {
        "title": "Star Wars: TextSearch Saga",
        "wikitext": {"*": (
            "{{Top|rwm|can}}\n"
            "{{ComicBook\n"
            "|title=''Test Knights''\n"
            "|publisher=[[Marvel Comics]]\n"
            "|series=''[[Star Wars: TextSearch Saga]]''\n"
            "|issue=1\n"
            "}}\n"
        )},
    },
}


def _wp_route(req):
    qs = parse_qs(urlparse(str(req.url)).query)
    if qs.get("action", [None])[0] == "query" and qs.get("list", [None])[0] == "search":
        return httpx.Response(200, json=WOOKIEE_SEARCH_PAYLOAD)
    if qs.get("action", [None])[0] == "parse":
        return httpx.Response(200, json=WOOKIEE_PARSE_PAYLOAD)
    return httpx.Response(404)


@respx.mock
def test_wookieepedia_search_text_returns_candidate():
    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_wp_route)
    with _client():
        pass
    out = asyncio.run(wookieepedia.search_text("txtsearch-knights"))
    assert len(out) == 1
    assert out[0].source == "wookieepedia"
    assert out[0].issue_number == "1"


# ---------------------------------------------------------------------------
# Aggregator + pagination route
# ---------------------------------------------------------------------------


@respx.mock
def test_aggregator_search_text_fans_out_to_three_sources():
    respx.get("https://comicvine.gamespot.com/api/search/").mock(
        return_value=httpx.Response(200, json=CV_SEARCH_PAYLOAD)
    )
    respx.get("https://metron.cloud/api/issue/").mock(
        return_value=httpx.Response(200, json=METRON_LIST_PAYLOAD)
    )
    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_wp_route)
    with _client():
        pass
    result = asyncio.run(search_text("txtsearch-zzx"))
    sources = sorted({c.source for c in result.candidates})
    assert sources == ["comicvine", "metron", "wookieepedia"]
    assert not result.rate_limited


@respx.mock
def test_text_search_route_renders_picker_with_results():
    respx.get("https://comicvine.gamespot.com/api/search/").mock(
        return_value=httpx.Response(200, json=CV_SEARCH_PAYLOAD)
    )
    respx.get("https://metron.cloud/api/issue/").mock(
        return_value=httpx.Response(200, json=METRON_LIST_PAYLOAD)
    )
    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_wp_route)

    with _client() as client:
        r = client.post("/add/text-search", data={"q": "txtsearch-zzx"})
        assert r.status_code == 200
        # Header reports total + page count.
        assert "result" in r.text
        # All three sources appear.
        assert "comicvine" in r.text
        assert "metron" in r.text
        assert "wookieepedia" in r.text


@respx.mock
def test_text_search_pagination_slices_aggregate():
    """Build a payload of 25 CV issues so we get 3 pages of 12-12-1."""
    big_payload = {
        "results": [
            {"id": i, "name": f"Issue {i}", "issue_number": str(i),
             "volume": {"name": "Big Series"},
             "image": {"super_url": f"https://cv/{i}.jpg"},
             "publisher": {"name": "Big Pub"}}
            for i in range(1, 26)
        ],
    }
    respx.get("https://comicvine.gamespot.com/api/search/").mock(
        return_value=httpx.Response(200, json=big_payload)
    )
    respx.get("https://metron.cloud/api/issue/").mock(
        return_value=httpx.Response(200, json={"count": 0, "next": None, "results": []})
    )
    respx.get("https://starwars.fandom.com/api.php").mock(
        return_value=httpx.Response(200, json={"query": {"search": []}})
    )

    with _client() as client:
        # ComicVine returns 25 candidates (capped via TEXT_SEARCH_LIMIT=20),
        # but only 20 actually come through because of the limit. Page 1
        # shows 12 of 20.
        r = client.post("/add/text-search", data={"q": "txtsearch-bb"})
        assert "page 1 of 2" in r.text.lower()
        assert "Issue 1" in r.text
        # Pagination button hits the GET endpoint.
        assert "/add/text-search?q=txtsearch-bb&amp;page=2" in r.text or "/add/text-search?q=txtsearch-bb&page=2" in r.text

        r2 = client.get("/add/text-search", params={"q": "txtsearch-bb", "page": 2})
        assert "page 2 of 2" in r2.text.lower()
        # Page 2 should show entries beyond the first 12.
        assert "Issue 13" in r2.text


def test_text_search_with_empty_query_renders_landing():
    with _client() as client:
        r = client.post("/add/text-search", data={"q": ""})
        assert r.status_code == 200
        assert "Type a title or series" in r.text


def test_add_page_shows_text_search_form():
    with _client() as client:
        r = client.get("/add")
        assert r.status_code == 200
        assert 'hx-post="/add/text-search"' in r.text
        assert "Or search by title / series" in r.text
