"""Wookieepedia client tests."""

import asyncio
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import wookieepedia
from app.services.wookieepedia import (
    _clean,
    _extract_contents_section,
    _find_infobox,
    _parse_date,
)


# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------


def test_clean_strips_links_refs_and_emphasis():
    raw = "[[Marc Guggenheim]]<ref name=\"x\">cite</ref> and ''italic'' text"
    assert _clean(raw) == "Marc Guggenheim and italic text"


def test_clean_handles_piped_link_and_self_closing_ref():
    raw = "[[Star Wars (2020)|''Star Wars'' (2020)]]<ref name=\"x\" />"
    assert _clean(raw) == "Star Wars (2020)"


def test_clean_strips_br_tags_and_leading_bullets():
    assert _clean("Vol. 2 –<br />A Higher Path") == "Vol. 2 – A Higher Path"
    assert _clean("*''[[Star Wars: Jedi Knights]]''") == "Star Wars: Jedi Knights"
    assert _clean("*A\n*B") == "A\nB"


def test_parse_date_full_and_year_only():
    assert _parse_date("April 14, 2026") == "2026-04-14"
    assert _parse_date("July 1977") == "1977"
    assert _parse_date("nothing") is None


def test_find_infobox_picks_comicbook_template():
    wt = (
        "{{Top|rwm|can}}\n"
        "{{ComicBook\n"
        "|title=''Jedi Knights'' 1\n"
        "|writer=[[Marc Guggenheim]]\n"
        "|publisher=[[Marvel Comics]]\n"
        "|series=''[[Star Wars: Jedi Knights]]''\n"
        "|issue=1\n"
        "|pages=26\n"
        "|release date=[[March 5]], [[2025]]\n"
        "|image=[[File:Jedi-Knights-1-Final-Cover.jpg]]\n"
        "}}\n"
        "{{Reflist}}\n"
    )
    fields = _find_infobox(wt)
    assert fields is not None
    assert fields["__template__"] == "ComicBook"
    assert fields["title"] == "Jedi Knights 1"
    assert fields["writer"] == "Marc Guggenheim"
    assert fields["publisher"] == "Marvel Comics"
    assert fields["series"] == "Star Wars: Jedi Knights"
    assert fields["issue"] == "1"
    assert fields["pages"] == "26"


def test_extract_contents_section_parses_bullet_list():
    wt = (
        "==Contents==\n"
        "*''[[Darth Vader (2020) 42|''Darth Vader'' (2020) 42]]''\n"
        "*''[[Darth Vader (2020) 43|''Darth Vader'' (2020) 43]]''\n"
        "==Appearances==\n"
    )
    assert _extract_contents_section(wt) == [
        "Darth Vader (2020) 42",
        "Darth Vader (2020) 43",
    ]


def test_extract_contents_section_falls_back_to_gallery():
    """Newer Marvel epic-style volumes list their collected issues as a
    `<gallery>` block inside `==Contents==` instead of a bullet list —
    the parser falls back to gallery extraction when no bullets exist."""
    wt = (
        "==Contents==\n"
        "<gallery captionalign=\"center\">\n"
        "File:DarthVader2020-23-textless.jpg|[[Darth Vader (2020) 23|"
        "''Darth Vader'' (2020) 23]]\n"
        "File:DarthVader2020-24-Textless.png|[[Darth Vader (2020) 24|"
        "''Darth Vader'' (2020) 24]]\n"
        "</gallery>\n"
        "==Appearances==\n"
    )
    assert _extract_contents_section(wt) == [
        "Darth Vader (2020) 23",
        "Darth Vader (2020) 24",
    ]


def test_extract_contents_section_gallery_scoped_to_section():
    """Gallery fallback is scoped to the ==Contents== body — an
    unrelated cover gallery elsewhere on the page must not leak in."""
    wt = (
        "==Contents==\n"
        "<gallery>\n"
        "File:a.jpg|[[Darth Vader (2020) 23|Vader 23]]\n"
        "</gallery>\n"
        "==Cover gallery==\n"
        "<gallery>\n"
        "File:b.jpg|[[Some Other Comic 99|Other 99]]\n"
        "</gallery>\n"
    )
    assert _extract_contents_section(wt) == ["Darth Vader (2020) 23"]


def test_find_infobox_returns_none_for_pages_without_one():
    wt = "{{Top|rwm|can}}\n{{Reflist}}\n"
    assert _find_infobox(wt) is None


def test_find_infobox_recognises_comicstory():
    """{{ComicStory}} infoboxes are parsed so series inference can read
    the `series=` field of a short story collected inside a trade."""
    wt = (
        "{{Top|rwm|can}}\n"
        "{{ComicStory\n"
        "|title=\"Ring Race\"\n"
        "|writer=[[Martin Fisher]]\n"
        "|series=[[Star Wars Rebels Magazine#Comics|Rebels Magazine]]\n"
        "}}\n"
    )
    fields = _find_infobox(wt)
    assert fields is not None
    assert fields["__template__"] == "ComicStory"


# A ComicStory article whose series= points at a section anchor.
COMICSTORY_PARSE = {
    "parse": {
        "title": "Ring Race",
        "wikitext": {"*": (
            "{{Top|rwm|can}}\n"
            "{{ComicStory\n"
            "|title=\"Ring Race\"\n"
            "|series=[[Star Wars Rebels Magazine#Comics|''Rebels Magazine'']]\n"
            "}}\n"
        )},
    }
}


@respx.mock
def test_get_article_resolves_comicstory_and_strips_section_anchor():
    """A collected short story resolves to its series; a `#section`
    anchor stays on `series_article_id` but is stripped off the name."""
    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json=COMICSTORY_PARSE)
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with TestClient(create_app()):  # ensure the cache table exists
        c = asyncio.run(wookieepedia.get_article("Ring Race"))
    assert c is not None
    assert c.series == "Star Wars Rebels Magazine"
    assert c.series_article_id == "Star Wars Rebels Magazine#Comics"


@respx.mock
def test_search_does_not_surface_comicstory_articles():
    """ComicStory articles are parseable but must not appear as
    addable comics in the /add picker."""
    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "query" and qs.get("list", [None])[0] == "search":
            return httpx.Response(200, json={
                "query": {"search": [{"title": "Ring Race"}]},
            })
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json=COMICSTORY_PARSE)
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with TestClient(create_app()):  # ensure cache table exists
        results = asyncio.run(wookieepedia.search_text("Ring Race"))
    assert results == []


# ---------------------------------------------------------------------------
# End-to-end: search → parse → image
# ---------------------------------------------------------------------------


SEARCH_HIT = {
    "query": {"search": [{"title": "Jedi Knights 1", "pageid": 999}]}
}

PARSE_HIT = {
    "parse": {
        "title": "Jedi Knights 1",
        "wikitext": {
            "*": (
                "{{Top|rwm|can}}\n"
                "{{ComicBook\n"
                "|image=[[File:Jedi-Knights-1-Final-Cover.jpg]]\n"
                "|title=''Jedi Knights'' 1\n"
                "|writer=[[Marc Guggenheim]]\n"
                "|publisher=[[Marvel Comics]]\n"
                "|release date=[[March 5]], [[2025]]\n"
                "|pages=26\n"
                "|upc=75960621106700111\n"
                "|series=''[[Star Wars: Jedi Knights]]''\n"
                "|issue=1\n"
                "}}\n"
            )
        },
    }
}

IMAGE_HIT = {
    "query": {
        "pages": {
            "-1": {
                "title": "File:Jedi-Knights-1-Final-Cover.jpg",
                "imageinfo": [
                    {"url": "https://static.wikia.nocookie.net/jk1.jpg"}
                ],
            }
        }
    }
}


def _route_api(request: httpx.Request) -> httpx.Response:
    qs = parse_qs(urlparse(str(request.url)).query)
    action = qs.get("action", [None])[0]
    if action == "query" and "list" in qs and qs["list"][0] == "search":
        return httpx.Response(200, json=SEARCH_HIT)
    if action == "parse":
        return httpx.Response(200, json=PARSE_HIT)
    if action == "query" and "prop" in qs and "imageinfo" in qs["prop"][0]:
        return httpx.Response(200, json=IMAGE_HIT)
    return httpx.Response(404, json={"error": "unrouted"})


@respx.mock
def test_search_isbn_returns_one_candidate_with_resolved_cover():
    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route_api)
    [c] = asyncio.run(wookieepedia.search_isbn("9781302963217"))
    assert c.source == "wookieepedia"
    assert c.title == "Jedi Knights 1"
    assert c.publisher == "Marvel Comics"
    assert c.series == "Star Wars: Jedi Knights"
    assert c.issue_number == "1"
    assert c.page_count == 26
    assert c.cover_date == "2025-03-05"
    assert c.cover_url == "https://static.wikia.nocookie.net/jk1.jpg"
    # source_id is the article title for navigation back to the wiki
    assert c.source_id == "Jedi Knights 1"


@respx.mock
def test_search_upc_uses_same_pipeline():
    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route_api)
    [c] = asyncio.run(wookieepedia.search_upc("75960621106700111"))
    assert c.source == "wookieepedia"
    assert c.title == "Jedi Knights 1"


@respx.mock
def test_search_returns_empty_list_when_no_hits():
    respx.get("https://starwars.fandom.com/api.php").mock(
        return_value=httpx.Response(200, json={"query": {"search": []}})
    )
    assert asyncio.run(wookieepedia.search_isbn("0000000000000")) == []
