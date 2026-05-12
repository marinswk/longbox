"""Phase 11b: creators persistence + auto-tags on save."""

import asyncio

import httpx
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import wookieepedia


def _client() -> TestClient:
    return TestClient(create_app())


# ---------------------------------------------------------------------------
# Wookieepedia infobox creator extraction
# ---------------------------------------------------------------------------

WIKITEXT_JK1 = (
    "{{Top|rwm|can}}\n"
    "{{ComicBook\n"
    "|image=[[File:Creators-Test-Cover.jpg]]\n"
    "|title=''Jedi Knights'' 1\n"
    "|writer=[[Marc Guggenheim]]\n"
    "|penciller=[[Madibek Musabekov]]\n"
    "|inker=Madibek Musabekov\n"
    "|letterer=[[Clayton Cowles]]\n"
    "|colorist=[[Luis Guerrero]]\n"
    "|cover artist=[[Rahzzah]]\n"
    "|editor=[[Mark Paniccia]]\n"
    "|publisher=[[Marvel Comics]]\n"
    "|series=''[[Star Wars: Jedi Knights]]''\n"
    "|issue=1\n"
    "|pages=26\n"
    "|release date=[[March 5]], [[2025]]\n"
    "}}\n"
)


def _wp_route(request: httpx.Request) -> httpx.Response:
    from urllib.parse import parse_qs, urlparse

    qs = parse_qs(urlparse(str(request.url)).query)
    action = qs.get("action", [None])[0]
    if action == "query" and "list" in qs and qs["list"][0] == "search":
        return httpx.Response(200, json={
            "query": {"search": [{"title": "Creators Test Article", "pageid": 999}]}
        })
    if action == "parse":
        return httpx.Response(200, json={
            "parse": {"title": "Creators Test Article", "wikitext": {"*": WIKITEXT_JK1}}
        })
    if action == "query" and "imageinfo" in (qs.get("prop", [""])[0]):
        return httpx.Response(200, json={"query": {"pages": {"-1": {"imageinfo": [{"url": "https://x/y.jpg"}]}}}})
    return httpx.Response(404)


@respx.mock
def test_wookieepedia_candidate_includes_seven_creators():
    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_wp_route)
    [c] = asyncio.run(wookieepedia.search_isbn("9781302963200"))
    by_role = {(cr.role, cr.name) for cr in c.creators}
    assert ("writer", "Marc Guggenheim") in by_role
    assert ("penciller", "Madibek Musabekov") in by_role
    assert ("inker", "Madibek Musabekov") in by_role
    assert ("letterer", "Clayton Cowles") in by_role
    assert ("colorist", "Luis Guerrero") in by_role
    assert ("cover artist", "Rahzzah") in by_role
    assert ("editor", "Mark Paniccia") in by_role


# ---------------------------------------------------------------------------
# /add/save persists creators + auto-tags 'star wars' from Wookieepedia
# ---------------------------------------------------------------------------


@respx.mock
def test_save_from_wookieepedia_persists_creators_and_auto_tags():
    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_wp_route)

    with _client() as client:
        r = client.post(
            "/add/save",
            data={
                "title": "Creators Test Article",
                "issue_number": "1",
                "publisher": "Marvel Comics",
                "series": "Star Wars: Jedi Knights",
                "isbn_13": "",
                "source": "wookieepedia",
                "source_id": "Creators Test Article",
            },
        )
        assert r.status_code == 200

        comics = client.get("/api/comics").json()
        cid = next(c["id"] for c in comics if c["title"] == "Creators Test Article")

        page = client.get(f"/comic/{cid}").text
        # Creators block rendered with all seven roles.
        assert "CREATORS" in page
        assert "Marc Guggenheim" in page
        assert "Clayton Cowles" in page
        assert "Rahzzah" in page
        # Auto-tag landed.
        assert "star wars" in page


# ---------------------------------------------------------------------------
# ComicVine person_credits extraction
# ---------------------------------------------------------------------------


@respx.mock
def test_comicvine_creators_from_person_credits():
    respx.get("https://comicvine.gamespot.com/api/issue/4000-12345/").mock(
        return_value=httpx.Response(200, json={"results": {
            "id": 12345,
            "name": "Saga #1",
            "issue_number": "1",
            "image": {"super_url": "https://cv/x.jpg"},
            "volume": {"name": "Saga"},
            "publisher": {"name": "Image Comics"},
            "person_credits": [
                {"name": "Brian K. Vaughan", "role": "writer"},
                {"name": "Fiona Staples", "role": "artist, colorist"},
            ],
        }})
    )
    respx.get("https://metron.cloud/api/issue/12345/").mock(return_value=httpx.Response(404))

    with _client() as client:
        body = client.get("/api/lookup", params={"q": "12345"}).json()
        cv = next(c for c in body["candidates"] if c["source"] == "comicvine")
        roles = {(cr["role"], cr["name"]) for cr in cv["creators"]}
        assert ("writer", "Brian K. Vaughan") in roles
        # Comma-separated roles split out.
        assert ("artist", "Fiona Staples") in roles
        assert ("colorist", "Fiona Staples") in roles


# ---------------------------------------------------------------------------
# Metron credits extraction
# ---------------------------------------------------------------------------


@respx.mock
def test_metron_creators_from_credits_list():
    respx.get("https://comicvine.gamespot.com/api/issue/4000-77/").mock(return_value=httpx.Response(404))
    respx.get("https://metron.cloud/api/issue/77/").mock(
        return_value=httpx.Response(200, json={
            "id": 77,
            "name": "Doctor Aphra 1",
            "number": "1",
            "image": "https://m/x.jpg",
            "series": {"name": "Doctor Aphra", "publisher": {"name": "Marvel Comics"}},
            "credits": [
                {"creator": {"name": "Alyssa Wong"}, "role": [{"name": "writer"}]},
                {"creator": {"name": "Marika Cresta"}, "role": [{"name": "penciller"}, {"name": "inker"}]},
            ],
        })
    )

    with _client() as client:
        body = client.get("/api/lookup", params={"q": "77"}).json()
        m = next(c for c in body["candidates"] if c["source"] == "metron")
        roles = {(cr["role"], cr["name"]) for cr in m["creators"]}
        assert ("writer", "Alyssa Wong") in roles
        assert ("penciller", "Marika Cresta") in roles
        assert ("inker", "Marika Cresta") in roles
