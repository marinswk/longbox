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


# Hierarchical `series=` infobox: level-1 is the broad franchise,
# level-2 is the specific comic series the issue belongs to. The
# parser must pick the level-2 entry, not the franchise — otherwise
# /series/{id} ends up linked to a multimedia-overview wiki article
# with no Issues/Volumes section to extract.

HIERARCHICAL_SERIES_WIKITEXT = (
    "{{Top|rwm}}\n"
    "{{ComicCollection\n"
    "|title=''Monster of Temple Peak and Other Stories''\n"
    "|publisher=[[Dark Horse Comics]]\n"
    "|media type=Trade paperback\n"
    "|series=*''[[Star Wars: The High Republic]]'' {{C|[[Phase I: Light of the Jedi|Phase I]]}}\n"
    "**''[[Star Wars: The High Republic Adventures — The Monster of Temple Peak]]''\n"
    "**[[Star Wars: The High Republic Adventures (2021)|''Star Wars: The High Republic Adventures'' (2021)]]\n"
    "}}\n"
    "Some prose.\n"
)


@respx.mock
def test_candidate_picks_specific_series_from_hierarchical_infobox():
    """Regression for /series/4: the TPB infobox's `series=` field is
    a nested bullet list. Level-1 (`*`) is the broad franchise; level-2
    (`**`) is the specific comic series. We need the level-2 entry,
    otherwise the series gets linked to a multimedia franchise article
    that has no comic-series structure to parse."""
    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json={
                "parse": {"title": "Monster of Temple Peak and Other Stories",
                          "wikitext": {"*": HIERARCHICAL_SERIES_WIKITEXT}},
            })
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with _client():
        pass
    cand = asyncio.run(
        wookieepedia.get_article("Monster of Temple Peak and Other Stories")
    )
    assert cand is not None
    # Specific level-2 entry — NOT the level-1 franchise.
    assert cand.series == "Star Wars: The High Republic Adventures — The Monster of Temple Peak"


EDITIONS_WIKITEXT = (
    "{{Top}}\n"
    "==Editions==\n"
    "*[[Foo Trade Vol. 1]]\n"
    "*[[Foo Trade Vol. 2]]\n"
    "==External links==\n"
)


# Epic Collection: <gallery> blocks under ===Legends=== / ===Canon===
# subheadings nested in ==Media==. No Volumes header, no bullet list.
# This is what fooled the earlier fix — the parser found the Volumes
# regex didn't match, fell straight to the empty Contents section,
# and returned [].

EPIC_COLLECTION_GALLERY_WIKITEXT = (
    "{{Top|rwm|can|leg}}\n"
    "Some prose.\n"
    "==Media==\n"
    "===Legends===\n"
    "<gallery captionalign=\"center\">\n"
    "File:LegendsEpicCollection-EmpireVol1.png|''[[Star Wars Legends Epic Collection: The Empire Vol. 1|Star Wars Legends<br />Epic Collection:<br />The Empire Vol. 1]]''<br />[[April 7]], [[2015]]\n"
    "File:LegendsEpicCollection-NewRepublicVol1.png|''[[Star Wars Legends Epic Collection: The New Republic Vol. 1|Star Wars Legends<br />Epic Collection:<br />The New Republic Vol. 1]]''<br />[[May 12]], 2015\n"
    "</gallery>\n"
    "===Canon===\n"
    "<gallery>\n"
    "File:ModernEra-WarOfTheBountyHuntersVol1.png|''[[Star Wars: Modern Era Epic Collection - War of the Bounty Hunters Vol. 1]]''<br />[[June 18]], [[2024]]\n"
    "</gallery>\n"
    "==Sources==\n"
)


@respx.mock
def test_get_series_issues_parses_gallery_blocks_for_epic_collection():
    """Regression for /series/3: Epic Collection lists its member
    volumes inside <gallery> blocks under ===Legends=== / ===Canon===
    subheadings, not bullet lists or a Volumes section. The parser
    must walk every gallery block in the wikitext."""
    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json={
                "parse": {"title": "Epic Collection", "wikitext": {"*": EPIC_COLLECTION_GALLERY_WIKITEXT}},
            })
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with _client():
        pass
    # Distinct article title so the MetadataCache from
    # test_get_series_issues_parses_volumes_section_for_tpb_series
    # (which also uses "Epic Collection") doesn't leak in.
    issues = asyncio.run(wookieepedia.get_series_issues("Epic Collection Gallery Probe"))
    assert issues == [
        "Star Wars Legends Epic Collection: The Empire Vol. 1",
        "Star Wars Legends Epic Collection: The New Republic Vol. 1",
        "Star Wars: Modern Era Epic Collection - War of the Bounty Hunters Vol. 1",
    ]


# Star Wars Omnibus: prettytable rows under ===Installments=== with
# the volume title in bold-italic, prefixed with `N. ` and wikilinked.

OMNIBUS_TABLE_WIKITEXT = (
    "{{Top|rwm}}\n"
    "==Media==\n"
    "===Installments===\n"
    "{|{{Prettytable}}\n"
    "! Cover||Omnibus Title||Pub. Date||Included Story Arcs\n"
    "|-\n"
    "|rowspan=\"4\"|[[File:Cover1.jpg|100px]]||rowspan=\"4\"|'''''1. [[Star Wars Omnibus: X-Wing Rogue Squadron Volume 1]]'''''||rowspan=\"4\"|[[June 7]], [[2006]]||''[[Star Wars: X-Wing: Rogue Leader]]''\n"
    "|-\n"
    "|''[[Star Wars: X-Wing Rogue Squadron: The Rebel Opposition]]''\n"
    "|-\n"
    "|rowspan=\"4\"|[[File:Cover2.jpg|100px]]||rowspan=\"4\"|'''''2. [[Star Wars Omnibus: X-Wing Rogue Squadron Volume 2]]'''''||rowspan=\"4\"|[[October 25]], [[2006]]||''[[Other Arc]]''\n"
    "|}\n"
    "==Sources==\n"
)


@respx.mock
def test_get_series_issues_parses_numbered_prettytable_for_omnibus():
    """Regression for /series/4: Star Wars Omnibus uses a wikitable
    where each volume is `'''''N. [[Article]]'''''`. The story-arc
    wikilinks in adjacent cells must NOT leak into the result —
    only the bold-italic numbered titles are volume articles."""
    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json={
                "parse": {"title": "Star Wars Omnibus", "wikitext": {"*": OMNIBUS_TABLE_WIKITEXT}},
            })
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with _client():
        pass
    issues = asyncio.run(wookieepedia.get_series_issues("Star Wars Omnibus"))
    assert issues == [
        "Star Wars Omnibus: X-Wing Rogue Squadron Volume 1",
        "Star Wars Omnibus: X-Wing Rogue Squadron Volume 2",
    ]


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
