"""Tag-pages browse view: /tags index + /tag/{name} redirect.

The library router already accepts ?tag=name; these endpoints just give
users a discoverable entry point so they don't have to type the URL.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "Test Publisher", "series": "Test Series"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


def test_tags_index_lists_all_tags_with_counts():
    with _client() as client:
        cid = _save(client, title="Tg1", isbn_13="9784000000001", series="Tg Series")
        client.post(f"/comic/{cid}/tags", data={"name": "favorites"})
        client.post(f"/comic/{cid}/tags", data={"name": "must-read"})

        r = client.get("/tags")
        assert r.status_code == 200
        assert "TAGS" in r.text
        assert "favorites" in r.text
        assert "must-read" in r.text
        # Each tag links into the filtered library view.
        assert 'href="/library?tag=favorites"' in r.text


def test_tags_index_empty_state():
    with _client() as client:
        r = client.get("/tags")
        assert r.status_code == 200
        # Either empty-state copy or just no chips — both are valid.
        # Smoke test: page renders without error and the section heading is there.
        assert "TAGS" in r.text


def test_tag_redirect_to_library_filter():
    with _client() as client:
        r = client.get("/tag/some%20tag", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/library?tag=some%20tag"


def test_nav_links_to_tags_page():
    with _client() as client:
        r = client.get("/library")
        assert r.status_code == 200
        assert 'href="/tags"' in r.text
