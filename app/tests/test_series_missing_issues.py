"""Series detail + missing-issues detection.

Covers the Wookieepedia issue-list parser, the owned/missing match
logic, and the rendered series detail page.
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
from app.models import Comic, Series
from app.services import wookieepedia


def _client() -> TestClient:
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Mock Wookieepedia API helpers
# ---------------------------------------------------------------------------

SERIES_WIKITEXT = (
    "{{Top|rwm|can|fotj}}\n"
    "{{ComicSeries\n"
    "|title=''Star Wars: Test Knights''\n"
    "|publisher=[[Marvel Comics]]\n"
    "}}\n"
    "Some prose.\n"
    "==Issues==\n"
    "*[[Test Knights 1]]\n"
    "*[[Test Knights 2]]\n"
    "*[[Test Knights 3]]\n"
    "*[[Test Knights 4]]\n"
    "*[[Test Knights 5]]\n"
    "==External links==\n"
    "*Some link\n"
)


def _make_route():
    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        action = qs.get("action", [None])[0]
        if action == "parse":
            return httpx.Response(200, json={
                "parse": {"title": "Star Wars: Test Knights", "wikitext": {"*": SERIES_WIKITEXT}},
            })
        return httpx.Response(404)

    return _route


# ---------------------------------------------------------------------------
# Wookieepedia fetcher
# ---------------------------------------------------------------------------


@respx.mock
def test_get_series_issues_parses_issues_section():
    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_make_route())
    # Touch the app once so the lifespan creates the DB tables (the
    # MetadataCache the wookieepedia client writes through).
    with _client():
        pass
    issues = asyncio.run(wookieepedia.get_series_issues("Star Wars: Test Knights"))
    assert issues == [
        "Test Knights 1",
        "Test Knights 2",
        "Test Knights 3",
        "Test Knights 4",
        "Test Knights 5",
    ]


# TPB / collection series like "Epic Collection" or "Marvel Omnibus"
# don't have an ==Issues== section — their volumes live under
# ==Volumes==, ==Editions==, ==Trade paperbacks==, etc. The parser
# falls through to the volumes-headers branch for these.

EPIC_COLLECTION_WIKITEXT = (
    "{{Top|rwm|can|leg}}\n"
    "{{ComicSeries\n"
    "|title=''Star Wars: Epic Collection''\n"
    "|publisher=[[Marvel Comics]]\n"
    "}}\n"
    "Some intro prose.\n"
    "==Volumes==\n"
    "*[[Star Wars: Epic Collection - Vintage Vol. 1|Vintage Vol. 1]]\n"
    "*[[Star Wars: Epic Collection - Tales of the Jedi Vol. 1]]\n"
    "*[[Star Wars: Epic Collection - The Original Marvel Years Vol. 1]]\n"
    "==See also==\n"
    "*Other thing\n"
)


@respx.mock
def test_get_series_issues_parses_volumes_section_for_tpb_series():
    """Regression: TPB-collection series on Wookieepedia list their
    member volumes under ==Volumes== instead of ==Issues==. Without
    this branch the series detail page showed a blank
    expected-issues list for Epic Collection / Marvel Omnibus /
    Modern Era / similar TPB-series articles."""
    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json={
                "parse": {"title": "Epic Collection", "wikitext": {"*": EPIC_COLLECTION_WIKITEXT}},
            })
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with _client():
        pass
    issues = asyncio.run(wookieepedia.get_series_issues("Epic Collection"))
    assert issues == [
        "Star Wars: Epic Collection - Vintage Vol. 1",
        "Star Wars: Epic Collection - Tales of the Jedi Vol. 1",
        "Star Wars: Epic Collection - The Original Marvel Years Vol. 1",
    ]


EDITIONS_WIKITEXT = (
    "{{Top}}\n"
    "==Editions==\n"
    "*[[Foo Trade Vol. 1]]\n"
    "*[[Foo Trade Vol. 2]]\n"
    "==External links==\n"
)


@respx.mock
def test_get_series_issues_recognises_editions_header_variant():
    """Alternate heading: some Wookieepedia trade series use
    ==Editions== instead of ==Volumes==. Same parser path."""
    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json={
                "parse": {"title": "Foo", "wikitext": {"*": EDITIONS_WIKITEXT}},
            })
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with _client():
        pass
    issues = asyncio.run(wookieepedia.get_series_issues("Foo"))
    assert issues == ["Foo Trade Vol. 1", "Foo Trade Vol. 2"]


@respx.mock
def test_get_series_issues_returns_empty_for_missing_article():
    def _route(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"parse": None})

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with _client():
        pass
    issues = asyncio.run(wookieepedia.get_series_issues("Nonexistent-Series-Article"))
    assert issues == []


# ---------------------------------------------------------------------------
# /series/{id}/refresh + page render
# ---------------------------------------------------------------------------


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "Test Publisher", "series": "Test Series"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    return next(
        c["id"]
        for c in client.get("/api/comics", params={"limit": 500}).json()
        if c.get("isbn_13") == data.get("isbn_13")
    )


async def _series_id_for(name: str) -> int:
    async with SessionLocal() as session:
        row = (await session.exec(select(Series).where(Series.name == name))).first()
        assert row is not None, f"series {name!r} not found"
        return row.id


@respx.mock
def test_refresh_series_pulls_issue_list_and_persists():
    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_make_route())

    with _client() as client:
        # Seed two of the five expected issues.
        _save(client, title="Test Knights 1", issue_number="1",
              isbn_13="9785000000001", series="Knights of Test")
        _save(client, title="Test Knights 3", issue_number="3",
              isbn_13="9785000000003", series="Knights of Test")

        sid = asyncio.run(_series_id_for("Knights of Test"))

        r = client.post(
            f"/series/{sid}/refresh",
            data={"source": "wookieepedia",
                  "source_id": "Star Wars: Test Knights"},
        )
        assert r.status_code == 204
        assert r.headers.get("HX-Refresh") == "true"

        page = client.get(f"/series/{sid}").text
        # Header line shows owned 2 / 5.
        assert "2</span> / 5" in page or "Owned " in page
        # Owned issues are rendered with a checkmark + comic link.
        assert "Test Knights 1" in page
        assert "Test Knights 3" in page
        # Missing entries appear too, since they're still in the expected list.
        assert "Test Knights 2" in page
        assert "Test Knights 5" in page


@respx.mock
def test_series_page_handles_no_expected_issues_yet():
    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_make_route())

    with _client() as client:
        _save(client, title="Solo Bait", issue_number="1",
              isbn_13="9785000000999", series="Lonely Series")

        sid = asyncio.run(_series_id_for("Lonely Series"))
        r = client.get(f"/series/{sid}")
        assert r.status_code == 200
        # No issue list pulled yet — the empty-state copy and refresh form
        # should be visible.
        assert "No issue list pulled yet" in r.text
        assert "REFRESH FROM SOURCE" in r.text


def test_refresh_rejects_unsupported_source():
    with _client() as client:
        _save(client, title="X", issue_number="1",
              isbn_13="9785000000777", series="Other Series")
        sid = asyncio.run(_series_id_for("Other Series"))
        r = client.post(
            f"/series/{sid}/refresh",
            data={"source": "marvel", "source_id": "1234"},
        )
        assert r.status_code == 400
        assert "unsupported" in r.text


@respx.mock
def test_trade_collecting_issues_credits_series_progress():
    """A trade paperback whose collected_issues field includes the expected
    article titles should count those issues as owned on the series page,
    even if the user doesn't own the single issues themselves."""
    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_make_route())

    with _client() as client:
        # Save a single comic in the series whose only ownership of issues
        # 1-3 is via a trade we'll attach below.
        cid = _save(client, title="Test Knights Vol. 1", issue_number=None,
                    isbn_13="9785000000444", series="Bound Series")

        # Manually attach collected_issues to the saved comic so we don't
        # depend on a refresh round-trip in this test.
        async def _attach():
            async with SessionLocal() as session:
                comic = (await session.exec(
                    select(Comic).where(Comic.title == "Test Knights Vol. 1")
                )).first()
                comic.collected_issues = "Test Knights 1\nTest Knights 2\nTest Knights 3"
                session.add(comic)
                await session.commit()
        asyncio.run(_attach())

        sid = asyncio.run(_series_id_for("Bound Series"))
        client.post(
            f"/series/{sid}/refresh",
            data={"source": "wookieepedia",
                  "source_id": "Star Wars: Test Knights"},
        )

        page = client.get(f"/series/{sid}").text
        # 3 of 5 expected issues credited via the trade.
        assert "Owned <span class=\"text-crawl-dark\">3</span> / 5" in page
        # Trade-credited rows mention the trade title.
        assert "in" in page
        assert "Test Knights Vol. 1" in page


def test_comic_detail_links_to_series_page():
    with _client() as client:
        cid = _save(client, title="Linked Comic", issue_number="1",
                    isbn_13="9785000000222", series="Linked Series",
                    publisher="LinkPub")
        page = client.get(f"/comic/{cid}").text
        # Series name appears as a link to the series detail page.
        assert 'href="/series/' in page
        assert "Linked Series" in page
