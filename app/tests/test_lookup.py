import httpx
import respx
from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


CV_ISSUE = {
    "results": {
        "id": 305846,
        "name": "Saga #1",
        "issue_number": "1",
        "cover_date": "2012-03-14",
        "deck": "First issue.",
        "image": {"super_url": "https://cv.example/saga1.jpg"},
        "volume": {"name": "Saga"},
        "publisher": {"name": "Image Comics"},
    }
}


def _metron_issue_payload() -> dict:
    return {
        "id": 9999,
        "name": "Saga #1",
        "number": "1",
        "cover_date": "2012-03-14",
        "page": 24,
        "image": "https://metron.example/saga-1.jpg",
        "series": {"name": "Saga", "publisher": {"name": "Image Comics"}},
    }


def _ol_payload(isbn: str) -> dict:
    return {
        f"ISBN:{isbn}": {
            "title": "Saga, Volume One",
            "publishers": [{"name": "Image Comics"}],
            "publish_date": "2012",
            "number_of_pages": 160,
            "cover": {"large": "https://covers.openlibrary.example/saga1-L.jpg"},
            "key": "/books/OL12345M",
        }
    }


def _wookieepedia_no_hits(_request):
    return httpx.Response(200, json={"query": {"search": []}})


@respx.mock
def test_isbn_lookup_uses_wookieepedia_then_open_library():
    """ISBN flow runs Wookieepedia + OL in parallel; Wookieepedia first in
    the result list. Metron is never called for ISBN."""
    isbn = "9781607066010"
    metron_route = respx.get("https://metron.cloud/api/issue/").mock(
        return_value=httpx.Response(200, json={"count": 0, "results": []})
    )
    respx.get("https://starwars.fandom.com/api.php").mock(
        side_effect=_wookieepedia_no_hits
    )
    respx.get("https://openlibrary.org/api/books").mock(
        return_value=httpx.Response(200, json=_ol_payload(isbn))
    )

    with _client() as client:
        body = client.get("/api/lookup", params={"q": isbn}).json()
        assert body["kind"] == "isbn_13"
        assert [c["source"] for c in body["candidates"]] == ["openlibrary"]
        assert body["candidates"][0]["page_count"] == 160
        assert metron_route.call_count == 0


@respx.mock
def test_upc_lookup_routes_to_metron_and_wookieepedia():
    """A 12-digit numeric routes to UPC kind and queries Metron + Wookieepedia
    (skipping ComicVine, which has no UPC support, and Open Library, which is
    ISBN-only)."""
    upc = "759606096008"
    wp_route = respx.get("https://starwars.fandom.com/api.php").mock(
        side_effect=_wookieepedia_no_hits
    )
    ol_route = respx.get("https://openlibrary.org/api/books").mock(
        return_value=httpx.Response(200, json={})
    )
    metron_route = respx.get("https://metron.cloud/api/issue/").mock(
        return_value=httpx.Response(200, json={"count": 0, "next": None, "results": []})
    )

    with _client() as client:
        body = client.get("/api/lookup", params={"q": upc}).json()
        assert body["kind"] == "upc"
        assert wp_route.called
        assert metron_route.called
        assert ol_route.call_count == 0


@respx.mock
def test_isbn_lookup_returns_empty_when_open_library_misses():
    isbn = "9781000000777"
    respx.get("https://openlibrary.org/api/books").mock(
        return_value=httpx.Response(200, json={})
    )

    with _client() as client:
        body = client.get("/api/lookup", params={"q": isbn}).json()
        assert body["candidates"] == []


@respx.mock
def test_issue_id_lookup_returns_comicvine_first_then_metron():
    respx.get("https://comicvine.gamespot.com/api/issue/4000-305846/").mock(
        return_value=httpx.Response(200, json=CV_ISSUE)
    )
    respx.get("https://metron.cloud/api/issue/305846/").mock(
        return_value=httpx.Response(200, json=_metron_issue_payload())
    )

    with _client() as client:
        body = client.get("/api/lookup", params={"q": "305846"}).json()
        assert body["kind"] == "issue_id"
        assert [c["source"] for c in body["candidates"]] == ["comicvine", "metron"]


@respx.mock
def test_issue_id_lookup_metron_only_when_cv_404s():
    respx.get("https://comicvine.gamespot.com/api/issue/4000-9999/").mock(
        return_value=httpx.Response(404)
    )
    respx.get("https://metron.cloud/api/issue/9999/").mock(
        return_value=httpx.Response(200, json=_metron_issue_payload())
    )

    with _client() as client:
        body = client.get("/api/lookup", params={"q": "9999"}).json()
        assert [c["source"] for c in body["candidates"]] == ["metron"]


@respx.mock
def test_isbn_lookup_caches_subsequent_calls():
    isbn = "9781607066012"
    ol_route = respx.get("https://openlibrary.org/api/books").mock(
        return_value=httpx.Response(200, json=_ol_payload(isbn))
    )

    with _client() as client:
        client.get("/api/lookup", params={"q": isbn})
        client.get("/api/lookup", params={"q": isbn})

    assert ol_route.call_count == 1


@respx.mock
def test_isbn_lookup_degrades_when_open_library_errors():
    isbn = "9781607066013"
    respx.get("https://openlibrary.org/api/books").mock(return_value=httpx.Response(500))

    with _client() as client:
        body = client.get("/api/lookup", params={"q": isbn}).json()
        assert body["candidates"] == []
