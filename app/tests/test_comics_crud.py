from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_comics_crud_round_trip():
    with _client() as client:
        r = client.get("/api/comics")
        assert r.status_code == 200
        # Suite shares DB; just confirm endpoint works.
        assert isinstance(r.json(), list)

        r = client.post(
            "/api/comics",
            json={
                "title": "Saga #1",
                "issue_number": "1",
                "isbn_13": "9781607066019",
                "cover_price_eur": 2.99,
            },
        )
        assert r.status_code == 201
        created = r.json()
        cid = created["id"]
        assert created["title"] == "Saga #1"
        assert created["cover_price_eur"] == 2.99

        r = client.get(f"/api/comics/{cid}")
        assert r.status_code == 200
        assert r.json()["isbn_13"] == "9781607066019"

        r = client.patch(f"/api/comics/{cid}", json={"title": "Saga No. 1"})
        assert r.status_code == 200
        assert r.json()["title"] == "Saga No. 1"

        r = client.get("/api/comics")
        assert r.status_code == 200
        assert any(c["id"] == cid for c in r.json())

        r = client.delete(f"/api/comics/{cid}")
        assert r.status_code == 204

        r = client.get(f"/api/comics/{cid}")
        assert r.status_code == 404


def test_get_missing_returns_404():
    with _client() as client:
        r = client.get("/api/comics/999999")
        assert r.status_code == 404
