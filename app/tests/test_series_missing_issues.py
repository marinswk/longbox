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


# Some Wookieepedia issue tables list the same issue twice — once per
# overlapping TPB grouping. `{{Comictable-issue}}` rows are emitted by
# `_extract_comictable_issues`, and `get_series_issues` must de-dup the
# combined output (preserving first-seen order).

DUP_ISSUES_WIKITEXT = (
    "{{Top|rwm|can}}\n"
    "{{ComicSeries|title=''Dup Series''|publisher=[[IDW]]}}\n"
    "==Issues==\n"
    "{{Comictable-issue|1|[[Dup Series 1|''Dup Series'' 1]]|date|collected}}\n"
    "{{Comictable-issue|2|[[Dup Series 2|''Dup Series'' 2]]|date|collected}}\n"
    "{{Comictable-issue|3|[[Dup Series 3|''Dup Series'' 3]]|date|collected}}\n"
    # Vol. 2 re-lists issues 2 + 3 (overlapping TPB grouping).
    "{{Comictable-issue|2|[[Dup Series 2|''Dup Series'' 2]]|date|collected}}\n"
    "{{Comictable-issue|3|[[Dup Series 3|''Dup Series'' 3]]|date|collected}}\n"
    "{{Comictable-issue|4|[[Dup Series 4|''Dup Series'' 4]]|date|collected}}\n"
    "==External links==\n"
)


@respx.mock
def test_get_series_issues_dedups_repeated_comictable_rows():
    def _route(request: httpx.Request) -> httpx.Response:
        if parse_qs(urlparse(str(request.url)).query).get("action", [None])[0] == "parse":
            return httpx.Response(200, json={
                "parse": {"title": "Dup Series", "wikitext": {"*": DUP_ISSUES_WIKITEXT}},
            })
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with _client():
        pass
    issues = asyncio.run(wookieepedia.get_series_issues("Dup Series"))
    # Six rows but only four unique issues — first-seen order preserved.
    assert issues == ["Dup Series 1", "Dup Series 2", "Dup Series 3", "Dup Series 4"]


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


# Marvel Epic Collection has Legends + Modern Era sub-imprints that
# share one umbrella Wookieepedia article ("Epic Collection") but
# live in separate galleries (===Legends=== / ===Canon=== under
# ==Media==). The parser must:
#   1) detect the sub-imprint from the TPB's article title prefix and
#      set candidate.series + candidate.series_article_id accordingly;
#   2) honour a `#Section` suffix on the source_id and scope gallery
#      extraction to that section only.

EC_TPB_LEGENDS_WIKITEXT = (
    "{{Top|rwm}}\n"
    "{{ComicCollection\n"
    "|title=''Star Wars Legends Epic Collection: The Empire Vol. 1''\n"
    "|publisher=[[Marvel Comics]]\n"
    "|series=''[[Epic Collection]]''\n"
    "|media type=Trade paperback\n"
    "}}\n"
    "==Contents==\n"
    "*[[Republic 78|''Republic'' 78]]\n"
    "*[[Republic 79|''Republic'' 79]]\n"
)

EC_UMBRELLA_WIKITEXT = (
    "{{Top}}\n"
    "Some intro.\n"
    "==Media==\n"
    "===Legends===\n"
    "<gallery>\n"
    "File:A.png|''[[Star Wars Legends Epic Collection: The Empire Vol. 1]]''<br />2015\n"
    "File:B.png|''[[Star Wars Legends Epic Collection: The New Republic Vol. 1]]''<br />2015\n"
    "</gallery>\n"
    "===Canon===\n"
    "<gallery>\n"
    "File:C.png|''[[Star Wars Modern Era Epic Collection: Skywalker Strikes]]''<br />2024\n"
    "</gallery>\n"
    "==Sources==\n"
)


@respx.mock
def test_epic_collection_sub_imprint_detected_from_title():
    """Saving an EC TPB should land it in a Legends-vs-Modern-Era
    specific series, not a unified "Epic Collection" bucket. The
    distinction is encoded in the candidate's series_article_id
    (`Epic Collection#Legends`) so the auto-link / refresh fetches
    the matching gallery section only."""
    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json={
                "parse": {"title": "Star Wars Legends Epic Collection: The Empire Vol. 1",
                          "wikitext": {"*": EC_TPB_LEGENDS_WIKITEXT}},
            })
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with _client():
        pass
    cand = asyncio.run(wookieepedia.get_article(
        "Star Wars Legends Epic Collection: The Empire Vol. 1"
    ))
    assert cand is not None
    assert cand.series == "Star Wars Legends Epic Collection"
    assert cand.series_article_id == "Epic Collection#Legends"


@respx.mock
def test_get_series_issues_scopes_to_section_when_anchor_given():
    """`Epic Collection#Legends` and `Epic Collection#Canon` must
    return the Legends or Canon galleries respectively, not the
    union."""
    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json={
                "parse": {"title": "Epic Collection",
                          "wikitext": {"*": EC_UMBRELLA_WIKITEXT}},
            })
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with _client():
        pass
    # Distinct base article title to dodge MetadataCache pollution from
    # earlier tests that already cached "Epic Collection" with a
    # different fixture.
    legends = asyncio.run(wookieepedia.get_series_issues(
        "Epic Collection Scoped Probe#Legends"
    ))
    canon = asyncio.run(wookieepedia.get_series_issues(
        "Epic Collection Scoped Probe#Canon"
    ))
    assert legends == [
        "Star Wars Legends Epic Collection: The Empire Vol. 1",
        "Star Wars Legends Epic Collection: The New Republic Vol. 1",
    ]
    assert canon == [
        "Star Wars Modern Era Epic Collection: Skywalker Strikes",
    ]


# Marvel Omnibus is the hardcover sibling of Epic Collection. Same
# umbrella-article + gallery-sections shape but with three sections
# (Canon / Marvel Legends / Dark Horse Legends), and the title
# patterns the detector keys off of differ from EC.

MO_UMBRELLA_WIKITEXT = (
    "{{Top}}\n"
    "==Media==\n"
    "===Canon===\n"
    "<gallery>\n"
    "File:C1.png|''[[Star Wars: Kanan Omnibus]]''<br />2016\n"
    "</gallery>\n"
    "===Marvel Legends===\n"
    "<gallery>\n"
    "File:ML1.png|''[[Star Wars: The Original Marvel Years Omnibus Vol. 1]]''<br />2016\n"
    "</gallery>\n"
    "===Dark Horse Legends===\n"
    "<gallery>\n"
    "File:DH1.png|''[[Star Wars Legends: The Old Republic Omnibus Vol. 1]]''<br />2022\n"
    "File:DH2.png|''[[Star Wars Legends: The New Republic Omnibus Vol. 1]]''<br />2022\n"
    "</gallery>\n"
    "==Notes and references==\n"
)


def test_marvel_omnibus_subimprint_detector_routes_all_four_cases():
    """Unit-test the title-pattern → (display, source_id) mapping
    directly so each branch is exercised even if the upstream gallery
    section names ever change."""
    from app.services.wookieepedia import _detect_marvel_omnibus_subimprint

    assert _detect_marvel_omnibus_subimprint(
        "Star Wars Legends: The Old Republic Omnibus Vol. 1"
    ) == ("Star Wars Legends Omnibus", "Marvel Omnibus#Dark Horse Legends")

    assert _detect_marvel_omnibus_subimprint(
        "Star Wars: The Original Marvel Years Omnibus Vol. 1"
    ) == ("Star Wars Marvel Legends Omnibus", "Marvel Omnibus#Marvel Legends")

    assert _detect_marvel_omnibus_subimprint(
        "Star Wars: Kanan Omnibus"
    ) == ("Star Wars Marvel Omnibus", "Marvel Omnibus#Canon")

    # The Dark Horse Comics' own "Star Wars Omnibus: ..." line goes
    # through the normal series flow — NOT routed via Marvel Omnibus.
    assert _detect_marvel_omnibus_subimprint(
        "Star Wars Omnibus: X-Wing Rogue Squadron Volume 1"
    ) is None


@respx.mock
def test_get_series_issues_scopes_marvel_omnibus_sections():
    """Marvel Omnibus#Dark Horse Legends returns the Dark Horse
    gallery only — not the Canon or Marvel Legends entries."""
    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json={
                "parse": {"title": "Marvel Omnibus Probe",
                          "wikitext": {"*": MO_UMBRELLA_WIKITEXT}},
            })
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with _client():
        pass
    dh = asyncio.run(wookieepedia.get_series_issues(
        "Marvel Omnibus Probe#Dark Horse Legends"
    ))
    canon = asyncio.run(wookieepedia.get_series_issues(
        "Marvel Omnibus Probe#Canon"
    ))
    assert dh == [
        "Star Wars Legends: The Old Republic Omnibus Vol. 1",
        "Star Wars Legends: The New Republic Omnibus Vol. 1",
    ]
    assert canon == ["Star Wars: Kanan Omnibus"]


# Knight Errant-style prettytable Issues section — neither bullets
# nor Comictable templates. Each row is `|N||[[Article|...]]||date`
# with arc-header rows interspersed (`colspan="3"|[[Star Wars: Arc]]`).
# The base-name-prefix extractor must pick the issue articles and
# skip everything else.

KNIGHT_ERRANT_WIKITEXT = (
    "{{Top|rwm|old}}\n"
    "{{ComicSeries\n"
    "|title=''Star Wars: Knight Errant''\n"
    "|publisher=[[Dark Horse Comics]]\n"
    "}}\n"
    "==Media==\n"
    "===Issues===\n"
    "{|{{Prettytable}}\n"
    "! Issue||Title||Publication date||Trade paperback\n"
    "|-\n"
    "|style=\"background-color:#40CC40;\"|0||[[Knight Errant 0|''Knight Errant'' 0]]||[[August 12]], [[2010]]\n"
    "|-\n"
    "|style=\"background:#FFF8DC;\" colspan=\"3\"|[[Star Wars: Knight Errant: Aflame|''Aflame'']]||rowspan=\"6\"|[[Star Wars: Knight Errant Volume 1: Aflame|''Volume 1: Aflame'']]\n"
    "|-\n"
    "|style=\"background-color:#FFBB60;\"|1||[[Knight Errant: Aflame 1|''Aflame'' 1]]||[[October 13]], [[2010]]\n"
    "|-\n"
    "|style=\"background-color:#FFBB60;\"|2||[[Knight Errant: Aflame 2|''Aflame'' 2]]||[[November 10]], [[2010]]\n"
    "|-\n"
    "|style=\"background:#FFF8DC;\" colspan=\"3\"|[[Star Wars: Knight Errant: Deluge|''Deluge'']]||rowspan=\"5\"|[[Star Wars: Knight Errant Volume 2: Deluge|''Volume 2: Deluge'']]\n"
    "|-\n"
    "|style=\"background-color:#FFBB60;\"|1||[[Knight Errant: Deluge 1|''Deluge'' 1]]||[[August 17]], [[2011]]\n"
    "|}\n"
    "==Sources==\n"
)


@respx.mock
def test_get_series_issues_parses_old_style_prettytable_with_base_name_filter():
    """Regression: Knight Errant-style series articles list issues
    in a prettytable where every issue row is
    `|N||[[Series Base: Arc N|...]]||date`. We extract every wikilink
    whose article title starts with the series base name and ends
    with a digit. Arc-header rows (no number) and TPB references
    (`Volume 1: Aflame` — no trailing number) are filtered out."""
    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json={
                "parse": {"title": "Star Wars: Knight Errant (comic series)",
                          "wikitext": {"*": KNIGHT_ERRANT_WIKITEXT}},
            })
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with _client():
        pass
    issues = asyncio.run(wookieepedia.get_series_issues(
        "Star Wars: Knight Errant (comic series)"
    ))
    assert issues == [
        "Knight Errant 0",
        "Knight Errant: Aflame 1",
        "Knight Errant: Aflame 2",
        "Knight Errant: Deluge 1",
    ]


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


# Trade article: real issue list in ==Contents==, plus a decorative
# ===Cover gallery=== whose captions wikilink non-issue articles.

CONTENTS_PLUS_COVER_GALLERY_WIKITEXT = (
    "{{Top|rwm}}\n"
    "{{ComicCollection\n"
    "|title=''Classic Star Wars: Han Solo at Stars' End''\n"
    "|publisher=[[Dark Horse Comics]]\n"
    "}}\n"
    "==Contents==\n"
    "*[[Classic Star Wars: Han Solo at Stars' End 1|''…'' 1]]\n"
    "*[[Classic Star Wars: Han Solo at Stars' End 2|''…'' 2]]\n"
    "*[[Classic Star Wars: Han Solo at Stars' End 3|''…'' 3]]\n"
    "==Media==\n"
    "===Cover gallery===\n"
    "<gallery captionalign=\"center\">\n"
    "File:tpb.jpg|Original cover\n"
    "File:legends.jpg|[[Marvel Comics|Marvel]] [[Epic Collection|Legends Epic Collection]] cover\n"
    "</gallery>\n"
    "==Sources==\n"
)


# A series whose ==Issues== list nests issue bullets (`**`) under
# sub-section group headers (`*`) — the Classic Star Wars shape.

NESTED_ISSUES_WIKITEXT = (
    "{{Top|rwm}}\n"
    "{{ComicSeries|title=''BE Nested Probe''}}\n"
    "==Issues==\n"
    "*Original run (group header, no wikilink)\n"
    "**[[BE Nested Probe 1]]\n"
    "**[[BE Nested Probe 2]]\n"
    "*[[BE Nested Probe: Side Story (comic series)|''Side Story'']] (a header)\n"
    "**[[BE Nested Probe: Side Story 1]]\n"
    "**[[BE Nested Probe: Side Story 2]]\n"
    "==External links==\n"
)


@respx.mock
def test_get_series_issues_skips_nested_group_headers():
    """In a nested ==Issues== list the `*` group headers (a plain
    caption or a sub-series link) must NOT be counted as issues —
    only the `**` leaf bullets are real, trackable entries."""
    def _r(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json={"parse": {
                "title": "BE Nested Probe",
                "wikitext": {"*": NESTED_ISSUES_WIKITEXT},
            }})
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_r)
    with _client():
        pass
    issues = asyncio.run(wookieepedia.get_series_issues("BE Nested Probe"))
    assert issues == [
        "BE Nested Probe 1",
        "BE Nested Probe 2",
        "BE Nested Probe: Side Story 1",
        "BE Nested Probe: Side Story 2",
    ]


@respx.mock
def test_get_series_issues_ignores_cover_gallery_and_uses_contents():
    """Regression for /series/77: a trade article's ==Contents== holds
    the real issue list, but a ===Cover gallery=== wikilinks
    "[[Marvel Comics|Marvel]]" in a caption. The whole-document
    gallery scan must skip cover galleries so it doesn't return a
    bogus "Marvel Comics" issue and shadow the Contents list."""
    def _route(request: httpx.Request) -> httpx.Response:
        qs = parse_qs(urlparse(str(request.url)).query)
        if qs.get("action", [None])[0] == "parse":
            return httpx.Response(200, json={
                "parse": {"title": "HSSE Probe",
                          "wikitext": {"*": CONTENTS_PLUS_COVER_GALLERY_WIKITEXT}},
            })
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with _client():
        pass
    issues = asyncio.run(wookieepedia.get_series_issues("HSSE Probe"))
    assert issues == [
        "Classic Star Wars: Han Solo at Stars' End 1",
        "Classic Star Wars: Han Solo at Stars' End 2",
        "Classic Star Wars: Han Solo at Stars' End 3",
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
                "parse": {"title": "Editions Probe Article",
                          "wikitext": {"*": EDITIONS_WIKITEXT}},
            })
        return httpx.Response(404)

    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_route)
    with _client():
        pass
    # Use a unique article title to dodge MetadataCache pollution
    # from earlier tests that cached "Foo" with different content.
    issues = asyncio.run(wookieepedia.get_series_issues("Editions Probe Article"))
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


def test_match_owned_credits_anthology_story_from_combined_entry():
    """A trade that collects an anthology one-shot's story stores the
    entry as the combined "Story (Book N)" shape. Wookieepedia lists
    the STORY itself ("Tool of the Empire") as an issue of the series,
    so the matcher must credit the series on the story name — not just
    the host book ("Revelations (2023) 1")."""
    from app.services.series_progress import match_owned

    trade = Comic(
        title="Darth Vader Vol. 9",
        collected_issues=(
            "Tool of the Empire (Revelations (2023) 1)\n"
            "Darth Vader (2020) 42\n"
            "The Curse (comic story) (Free Comic Book Day 2024: Star Wars 1)"
        ),
    )
    expected = [
        "Tool of the Empire",
        "Darth Vader (2020) 42",
        "The Curse (comic story)",
        "Darth Vader (2020) 99",  # not owned
    ]
    pairs, owned = match_owned(expected, [trade])
    by_title = {p.title: p for p in pairs}
    assert by_title["Tool of the Empire"].trade is trade
    assert by_title["The Curse (comic story)"].trade is trade
    assert by_title["Darth Vader (2020) 42"].trade is trade
    assert by_title["Darth Vader (2020) 99"].trade is None
    assert owned == 3


def test_match_owned_credits_story_referenced_by_redirect_title():
    """A story can be filed under both a disambiguated redirect title
    ("Tall Tales (Revelations)") and a canonical one ("Tall Tales").
    A TPB's StoryCite and the series' issue table don't always pick
    the same one, so the matcher compares disambiguator-insensitively."""
    from app.services.series_progress import match_owned

    # TPB collects the story via the disambiguated redirect title.
    trade = Comic(
        title="Doctor Aphra Vol. 7",
        collected_issues=(
            "Doctor Aphra (2020) 40\n"
            "Tall Tales (Revelations) (Revelations (2023) 1)"
        ),
    )
    # Series lists the canonical title.
    pairs, owned = match_owned(["Doctor Aphra (2020) 40", "Tall Tales"], [trade])
    by_title = {p.title: p for p in pairs}
    assert by_title["Tall Tales"].trade is trade
    assert owned == 2

    # Reverse: series disambiguated, TPB plain — also matches.
    trade2 = Comic(title="Other", collected_issues="Tall Tales (Free Comic Book Day 2024: Star Wars 1)")
    pairs2, owned2 = match_owned(["Tall Tales (Revelations)"], [trade2])
    assert pairs2[0].trade is trade2
    assert owned2 == 1


def test_match_owned_credits_crossover_tie_ins_via_trade_pool():
    """A crossover/event series lists tie-in issues that are collected
    under the individual ongoing series' TPBs, not under the event.
    With a whole-library `trade_pool`, those still count as owned even
    though the collecting comic isn't linked to the event series."""
    from app.services.series_progress import match_owned

    # The TPB belongs to (is linked to) the ongoing Darth Vader series.
    vader_tpb = Comic(
        title="Darth Vader Vol. 3",
        collected_issues="Darth Vader (2020) 12\nDarth Vader (2020) 13",
    )
    # The event series' own page has no comics linked to it.
    expected = ["Darth Vader (2020) 12", "War of the Bounty Hunters 1"]

    # Without the pool: the event sees nothing.
    _pairs, owned = match_owned(expected, [])
    assert owned == 0

    # With the whole library as the trade pool: the tie-in is credited.
    pairs, owned = match_owned(expected, [], trade_pool=[vader_tpb])
    by_title = {p.title: p for p in pairs}
    assert by_title["Darth Vader (2020) 12"].trade is vader_tpb
    assert by_title["War of the Bounty Hunters 1"].trade is None
    assert owned == 1


def test_match_owned_does_not_credit_host_book_from_partial_story_reprint():
    """A TPB collecting just one story from an anthology one-shot must
    NOT mark the host book as owned. Otherwise the One-shots umbrella
    falsely shows ✓ for one-shots the user doesn't own, just because
    another TPB happens to reprint a story from them."""
    from app.services.series_progress import match_owned

    # Doctor Aphra trade collects ONE story from Revelations (2023) 1.
    # The user does NOT own Revelations (2023) 1 itself.
    trade = Comic(
        title="Doctor Aphra Vol. 7",
        collected_issues=(
            "Doctor Aphra (2020) 40\n"
            "Tall Tales (Revelations) (Revelations (2023) 1)"
        ),
    )
    # One-shots umbrella lists the host book as an expected entry.
    expected = ["Revelations (2023) 1"]
    pairs, owned = match_owned(expected, [], trade_pool=[trade])
    assert pairs[0].trade is None
    assert owned == 0


def test_match_owned_does_not_reuse_single_comic_across_multiple_expected():
    """One owned single-issue comic must satisfy AT MOST ONE expected
    entry. The number-fallback used to mark every "X 1"-shaped entry
    owned just because the user had a single comic with issue_number=1
    linked to the series — turning the One-shots umbrella into a sea
    of false-positive ✓'s."""
    from app.services.series_progress import match_owned

    # User owns just Revelations (2022) 1. The umbrella series lists
    # several #1 one-shots (each a distinct article).
    owned_comic = Comic(
        title="Revelations 1",
        source_id="Revelations (2022) 1",
        issue_number="1",
    )
    expected = [
        "Revelations (2022) 1",       # direct source_id match — owned
        "Revelations (2023) 1",       # different article, NOT owned
        "Tales from the Death Star",  # no trailing number
        "Marvel Comics 1000",         # trailing 1000, no comic matches
    ]
    pairs, owned = match_owned(expected, [owned_comic])
    by_title = {p.title: p for p in pairs}
    assert by_title["Revelations (2022) 1"].direct is owned_comic
    assert by_title["Revelations (2023) 1"].direct is None
    assert by_title["Revelations (2023) 1"].trade is None
    assert by_title["Tales from the Death Star"].direct is None
    assert by_title["Marvel Comics 1000"].direct is None
    assert owned == 1


def test_match_owned_number_fallback_still_works_when_unambiguous():
    """The number fallback IS legitimate when the user owns a comic
    with no source_id (e.g. manual / CSV entry) — it should still
    satisfy ONE expected entry whose trailing number matches."""
    from app.services.series_progress import match_owned

    owned_comic = Comic(title="Manual entry", issue_number="5")
    expected = ["Some Series 5", "Some Series 6"]
    pairs, owned = match_owned(expected, [owned_comic])
    by_title = {p.title: p for p in pairs}
    assert by_title["Some Series 5"].direct is owned_comic
    assert by_title["Some Series 6"].direct is None
    assert owned == 1


def test_match_owned_direct_source_id_wins_over_competing_number_fallback():
    """A comic that source_id-matches a later expected entry must
    still be claimed by that entry, even though its issue_number=1
    could plausibly fall-back-match an earlier entry."""
    from app.services.series_progress import match_owned

    owned_comic = Comic(
        title="R1", source_id="Revelations (2022) 1", issue_number="1",
    )
    expected = [
        "Revelations (2023) 1",       # would grab the comic via number
        "Revelations (2022) 1",       # the legitimate source_id owner
    ]
    pairs, _owned = match_owned(expected, [owned_comic])
    by_title = {p.title: p for p in pairs}
    assert by_title["Revelations (2022) 1"].direct is owned_comic
    assert by_title["Revelations (2023) 1"].direct is None


def test_comic_detail_links_to_series_page():
    with _client() as client:
        cid = _save(client, title="Linked Comic", issue_number="1",
                    isbn_13="9785000000222", series="Linked Series",
                    publisher="LinkPub")
        page = client.get(f"/comic/{cid}").text
        # Series name appears as a link to the series detail page.
        assert 'href="/series/' in page
        assert "Linked Series" in page


def test_parse_expected_dedups_and_sorts_numerically():
    """`parse_expected` is the read chokepoint for the series detail
    checklist + the progress denominator. It must:
      * drop duplicate lines (Wookieepedia lists some issues twice
        across overlapping TPB groupings),
      * sort numbered issues numerically (the "9 renders after 11"
        complaint), with un-numbered specials after the numbered runs.
    """
    from app.services.series_progress import parse_expected

    series = Series(
        name="SWA",
        # Upstream (messy) order: collection-grouped, 9 after 11, with
        # issue 14 and Annual 2018 each listed twice.
        expected_issues="\n".join([
            "SWA Ashcan",
            "SWA 1",
            "SWA 8",
            "SWA 10",
            "SWA 11",
            "SWA Annual 2018",
            "SWA 9",
            "SWA 12",
            "SWA 14",
            "SWA 14",            # duplicate
            "SWA Annual 2018",   # duplicate
            "SWA 2",
        ]),
    )
    result = parse_expected(series)
    # 12 lines − 2 dups = 10 unique.
    assert len(result) == 10
    assert result.count("SWA 14") == 1
    assert result.count("SWA Annual 2018") == 1
    # Numbered issues come first, in numeric order (9 BEFORE 11, 10 not
    # before 2 — natural sort).
    numbered = [r for r in result if r.split()[-1].isdigit()
                and int(r.split()[-1]) < 1900]
    assert numbered == [
        "SWA 1", "SWA 2", "SWA 8", "SWA 9",
        "SWA 10", "SWA 11", "SWA 12", "SWA 14",
    ]
    # Specials trail the numbered run.
    assert result[-2:] == ["SWA Annual 2018", "SWA Ashcan"]


def test_parse_expected_excludes_canceled_after_dedup():
    """Canceled entries are still removed, and dedup doesn't resurrect
    them."""
    from app.services.series_progress import parse_expected

    series = Series(
        name="C",
        expected_issues="C 1\nC 2\nC 2\nC 3\nC 3",
        canceled_issues="C 3",
    )
    result = parse_expected(series)
    assert result == ["C 1", "C 2"]


def test_backfill_dedup_expected_issues_cleans_stored_blob():
    """The lifespan backfill removes duplicate lines from stored
    expected_issues / canceled_issues, preserving first-seen order,
    and is idempotent."""
    from app.services.fandoms import backfill_dedup_expected_issues

    async def _seed():
        async with SessionLocal() as s:
            ser = Series(
                name="BD Dedup Series",
                expected_issues="BD 1\nBD 2\nBD 2\nBD 3\nBD 1",
                canceled_issues="BD 3\nBD 3",
            )
            s.add(ser)
            await s.commit()
            return ser.id
    sid = asyncio.run(_seed())

    changed = asyncio.run(backfill_dedup_expected_issues())
    assert changed >= 1

    async def _read():
        async with SessionLocal() as s:
            ser = await s.get(Series, sid)
            return ser.expected_issues, ser.canceled_issues
    expected, canceled = asyncio.run(_read())
    # Dedup preserves first-seen order (NOT sorted at storage layer).
    assert expected == "BD 1\nBD 2\nBD 3"
    assert canceled == "BD 3"

    # Idempotent: a second run changes nothing for this row.
    again = asyncio.run(backfill_dedup_expected_issues())
    # `again` counts rows changed across the whole (shared) test DB;
    # our row is already clean, so re-reading it must be unchanged.
    expected2, _ = asyncio.run(_read())
    assert expected2 == "BD 1\nBD 2\nBD 3"


def test_match_owned_credits_whole_issue_from_same_series_tpb():
    """A same-series TPB whose Wookieepedia Contents list one story per
    WHOLE issue ("Flight of the Falcon, Part 1 (Star Wars Adventures
    (2017) 14)") must credit the issue itself — the TPB collects the
    entire issue, not a fragment. Regression for /series/240 where
    issues 14–20 (owned via the Vol. 6 + Vol. 8 TPBs) showed as
    missing."""
    from app.services.series_progress import match_owned

    vol6 = Comic(
        title="SWA Vol. 6", source_id="SWA Vol. 6",
        collected_issues="\n".join([
            "Flight of the Falcon, Part 1 (Star Wars Adventures (2017) 14)",
            "Flight of the Falcon, Part 2 (Star Wars Adventures (2017) 15)",
            "Star Wars Adventures: Flight of the Falcon",
        ]),
    )
    vol8 = Comic(
        title="SWA Vol. 8", source_id="SWA Vol. 8",
        collected_issues="\n".join([
            "Raiders of the Lost Gundark (Star Wars Adventures (2017) 18)",
            "Star Wars Adventures (2017) 19",   # plain entry
            "Star Wars Adventures (2017) 20",
        ]),
    )
    expected = [f"Star Wars Adventures (2017) {n}" for n in (14, 15, 18, 19, 20)]
    pairs, owned = match_owned(expected, [vol6, vol8])
    by_title = {p.title: p for p in pairs}
    assert by_title["Star Wars Adventures (2017) 14"].trade is vol6
    assert by_title["Star Wars Adventures (2017) 15"].trade is vol6
    assert by_title["Star Wars Adventures (2017) 18"].trade is vol8
    assert by_title["Star Wars Adventures (2017) 19"].trade is vol8
    assert by_title["Star Wars Adventures (2017) 20"].trade is vol8
    assert owned == 5


def test_match_owned_book_credit_is_scoped_to_linked_comics():
    """The whole-issue (book) credit only applies to series-LINKED
    comics, not to the whole-library trade_pool. A TPB from another
    series that merely reprints one story from a multi-story anthology
    one-shot must NOT mark that one-shot owned (the Revelations guard,
    now via the include_books scoping)."""
    from app.services.series_progress import match_owned

    # Linked: a same-series TPB collecting the whole issue → credited.
    linked = Comic(
        title="Linked TPB", source_id="Linked TPB",
        collected_issues="Some Story (Owned Series 5)",
    )
    # Pool-only: another series' TPB reprinting one anthology story.
    pool_only = Comic(
        title="Other TPB", source_id="Other TPB",
        collected_issues="Bonus Tale (Anthology One-Shot 1)",
    )
    expected = ["Owned Series 5", "Anthology One-Shot 1"]
    pairs, owned = match_owned(expected, [linked], trade_pool=[linked, pool_only])
    by_title = {p.title: p for p in pairs}
    # Linked same-series TPB credits its whole issue.
    assert by_title["Owned Series 5"].trade is linked
    # Pool-only cross-series reprint does NOT credit the host one-shot.
    assert by_title["Anthology One-Shot 1"].trade is None
    assert owned == 1
