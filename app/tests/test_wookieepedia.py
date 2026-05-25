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
    _detect_movie_adaptation_series,
    _detect_oneshot_series,
    _extract_contents_section,
    _extract_cover_gallery,
    _find_infobox,
    _looks_like_comic_book_article,
    _parse_date,
    _splice_year_disambiguator,
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


def test_find_infobox_recognises_graphicnovel():
    """{{GraphicNovel}} articles (graphic novels / anthology trades
    like 'Tales from the Death Star') are parsed so they show up in
    search / ISBN lookup."""
    wt = (
        "{{Top|rwm|can}}\n"
        "{{GraphicNovel\n"
        "|title=''Tales from the Death Star''\n"
        "|publisher=[[Dark Horse Comics]]\n"
        "|isbn=9781506738291\n"
        "}}\n"
    )
    fields = _find_infobox(wt)
    assert fields is not None
    assert fields["__template__"] == "GraphicNovel"


def test_detect_oneshot_series_prefers_franchise_category():
    """A one-shot routes to a per-franchise '<X> — One-shots' series,
    preferring a Star Wars franchise category over the generic /
    publisher ones."""
    wt = (
        "body\n"
        "[[Category:Canon one-shot comics]]\n"
        "[[Category:Dark Horse Comics one-shot comics]]\n"
        "[[Category:Star Wars: The High Republic one-shot comics]]\n"
    )
    assert _detect_oneshot_series(wt) == (
        "Star Wars: The High Republic — One-shots",
        "Category:Star Wars: The High Republic one-shot comics",
    )


def test_detect_oneshot_series_falls_back_to_generic():
    wt = "body\n[[Category:Canon one-shot comics]]\n"
    assert _detect_oneshot_series(wt) == (
        "Star Wars — One-shots", "Category:Canon one-shot comics",
    )


def test_detect_oneshot_series_routes_free_comic_book_day():
    """FCBD comics get their own bucket — but a franchise one-shot
    category still takes precedence over it."""
    wt = "body\n[[Category:Free Comic Book Day comics]]\n"
    assert _detect_oneshot_series(wt) == (
        "Star Wars — Free Comic Book Day",
        "Category:Free Comic Book Day comics",
    )
    both = (
        "body\n[[Category:Free Comic Book Day comics]]\n"
        "[[Category:Star Wars: The High Republic one-shot comics]]\n"
    )
    assert _detect_oneshot_series(both)[0] == (
        "Star Wars: The High Republic — One-shots"
    )


def test_detect_oneshot_series_routes_graphic_novels():
    """An original graphic novel ('Hyperspace Stories: Qui-Gon') goes
    to a per-line '<X> — Graphic Novels' series, not the numbered
    issue series of the same franchise."""
    wt = "body\n[[Category:Star Wars: Hyperspace Stories graphic novels]]\n"
    assert _detect_oneshot_series(wt) == (
        "Star Wars: Hyperspace Stories — Graphic Novels",
        "Category:Star Wars: Hyperspace Stories graphic novels",
    )


def test_detect_oneshot_series_none_for_non_oneshot():
    assert _detect_oneshot_series("[[Category:2024 releases]]") is None


def test_find_infobox_recognises_book_template():
    """`{{Book}}` is generic and shared with prose novels — `_find_infobox`
    surfaces it, but `_candidate_from_title` gates it on categories that
    prove the article is a comic-format release."""
    wt = (
        "{{Top|rwm|can}}\n"
        "{{Book\n"
        "|title=''Star Wars: The Prequel Trilogy – A Graphic Novel''\n"
        "|author=[[Alessandro Ferrari]]\n"
        "|illustrator=[[Matteo Piana]]\n"
        "|publisher=[[Disney–Lucasfilm Press]]\n"
        "|isbn=9781368002745\n"
        "|isbn2=9781506746630\n"
        "}}\n"
    )
    fields = _find_infobox(wt)
    assert fields is not None
    assert fields["__template__"] == "Book"


def test_detect_movie_adaptation_series_routes_graphic_novel_to_umbrella():
    """A graphic-novel article in the 'Comic film adaptations' category
    (e.g. the Prequel / Original / Sequel Trilogy GNs) routes to the
    canonical 'Star Wars Movie Adaptations' umbrella article so all
    movie-adaptation comics cluster together."""
    wt = (
        "body\n"
        "[[Category:Canon graphic novels]]\n"
        "[[Category:Comic film adaptations]]\n"
    )
    assert _detect_movie_adaptation_series(
        wt, "Star Wars: The Original Trilogy – A Graphic Novel"
    ) == (
        "Star Wars Movie Adaptations",
        "Star Wars Movie Adaptations",
    )


def test_detect_movie_adaptation_series_routes_adaptation_miniseries():
    """A miniseries titled '…Adaptation' (e.g. Rogue One Adaptation,
    Force Awakens Adaptation) also routes to the umbrella."""
    wt = "body\n[[Category:Comic film adaptations]]\n"
    assert _detect_movie_adaptation_series(
        wt, "Star Wars: Rogue One Adaptation"
    ) == (
        "Star Wars Movie Adaptations",
        "Star Wars Movie Adaptations",
    )


def test_detect_movie_adaptation_series_rejects_tie_in_oneshot():
    """`Episode I: The Phantom Menace ½` carries the film-adaptation
    category for thematic reasons but isn't itself the adaptation
    comic. Without the title check, collected_issues inference drags
    the containing Epic Collection into the umbrella series."""
    wt = (
        "body\n"
        "[[Category:Comic film adaptations]]\n"
        "[[Category:Legends one-shot comics]]\n"
    )
    assert _detect_movie_adaptation_series(
        wt, "Episode I: The Phantom Menace ½"
    ) is None


def test_detect_movie_adaptation_series_none_when_category_missing():
    """Without the marker category, the umbrella fallback stays silent
    so unrelated articles aren't pulled into the movie-adaptation series."""
    wt = "body\n[[Category:Canon graphic novels]]\n"
    assert _detect_movie_adaptation_series(
        wt, "Star Wars: The Original Trilogy – A Graphic Novel"
    ) is None


def test_looks_like_comic_book_article_accepts_comic_categories():
    assert _looks_like_comic_book_article("[[Category:Canon graphic novels]]")
    assert _looks_like_comic_book_article("[[Category:Canon trade paperbacks]]")
    assert _looks_like_comic_book_article("[[Category:Marvel omnibuses]]")
    assert _looks_like_comic_book_article("[[Category:Comic film adaptations]]")


def test_looks_like_comic_book_article_rejects_prose_novel():
    """A typical Star Wars novel page has no GN/TPB/omnibus category —
    the `{{Book}}` infobox must not be parsed as a comic for these."""
    wt = (
        "[[Category:Canon novels]]\n"
        "[[Category:2017 novels]]\n"
        "[[Category:Hardcover books]]\n"
    )
    assert _looks_like_comic_book_article(wt) is False


def test_splice_year_disambiguator_inserts_before_issue_number():
    """Without this, "Revelations (2022) 1" and "Revelations (2023) 1"
    both end up as identical "Revelations 1" rows in the library."""
    assert _splice_year_disambiguator(
        "Revelations 1", "Revelations (2022) 1"
    ) == "Revelations (2022) 1"
    assert _splice_year_disambiguator(
        "Star Wars 1", "Star Wars (2020) 1"
    ) == "Star Wars (2020) 1"


def test_splice_year_disambiguator_appends_when_no_issue_number():
    assert _splice_year_disambiguator(
        "Tales of Something", "Tales of Something (2017)"
    ) == "Tales of Something (2017)"


def test_splice_year_disambiguator_no_op_when_year_already_present():
    """Idempotent — re-running on an already-spliced title is a no-op."""
    assert _splice_year_disambiguator(
        "Revelations (2022) 1", "Revelations (2022) 1"
    ) == "Revelations (2022) 1"


def test_splice_year_disambiguator_no_op_when_article_has_no_year():
    """Most Wookieepedia article titles have no year disambiguator
    (the work is uniquely named) — leave the displayed title alone."""
    assert _splice_year_disambiguator(
        "War of the Bounty Hunters 5", "War of the Bounty Hunters 5"
    ) == "War of the Bounty Hunters 5"


def test_extract_cover_gallery_returns_label_and_filename_for_each_entry():
    """Pull every File:NNN line out of every <gallery> inside every
    ==Cover gallery== section. Captions get _clean'd (wikilinks
    flattened) so the label is human-readable."""
    wt = (
        "lead paragraph\n"
        "==Publisher's summary==\n"
        "blah\n"
        "==Cover gallery==\n"
        "<gallery>\n"
        "File:WOTBH5-cover.jpg|[[Steve McNiven]] cover\n"
        "File:WBH5McNivenCarbonite.jpg|Carbonite variant\n"
        "File:WBH5Cassaday.jpg|Trading Card variant by [[John Cassaday]]\n"
        "</gallery>\n"
        "==Notes==\n"
        "footnotes\n"
    )
    out = _extract_cover_gallery(wt)
    assert [v["filename"] for v in out] == [
        "WOTBH5-cover.jpg",
        "WBH5McNivenCarbonite.jpg",
        "WBH5Cassaday.jpg",
    ]
    assert out[0]["label"] == "Steve McNiven cover"
    assert out[1]["label"] == "Carbonite variant"
    assert out[2]["label"] == "Trading Card variant by John Cassaday"


def test_extract_cover_gallery_returns_empty_when_section_missing():
    """Trades / pages without a cover gallery section return []."""
    wt = "lead\n==Publisher's summary==\nblah\n==Notes==\nfoot\n"
    assert _extract_cover_gallery(wt) == []


@respx.mock
def test_comicbook_template_defaults_format_to_single_issue():
    """`{{ComicBook}}` infoboxes rarely carry `media type=` upstream.
    Without a per-template default, single-issue imports landed with
    `format=None` and an empty Format column on /comic/{id}."""
    parse = {"parse": {"title": "SI 5", "wikitext": {"*": (
        "{{Top|rwm|can}}\n"
        "{{ComicBook\n"
        "|title=''SI 5''\n"
        "|writer=[[Someone]]\n"
        "|publisher=[[Marvel Comics]]\n"
        "|series=''[[Some Series]]''\n"
        "|issue=5 of 5\n"
        "}}\n"
    )}}}

    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json=parse)
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with TestClient(create_app()):
        c = asyncio.run(wookieepedia.get_article("SI 5"))
    assert c is not None
    assert c.format == "single issue"


@respx.mock
def test_graphicnovel_template_defaults_format_to_graphic_novel():
    """Mirrors the ComicBook default — `{{GraphicNovel}}` articles
    without `media type=` should still produce a meaningful format."""
    parse = {"parse": {"title": "GN", "wikitext": {"*": (
        "{{Top|rwm|can}}\n"
        "{{GraphicNovel\n"
        "|title=''GN''\n"
        "|publisher=[[Dark Horse Comics]]\n"
        "|isbn=9799000000001\n"
        "}}\n"
    )}}}

    def _route(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=parse)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with TestClient(create_app()):
        c = asyncio.run(wookieepedia.get_article("GN"))
    assert c is not None
    assert c.format == "graphic novel"


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
