"""Comic detail page + per-Copy CRUD tests."""

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    # `/api/comics` defaults to limit=50; the shared test DB grows
    # past that across runs so we need the full page to find a row by
    # ISBN — see the "test DB accumulates" note in CLAUDE.md.
    comics = client.get("/api/comics", params={"limit": 500}).json()
    cand = [c for c in comics if c.get("isbn_13") == data.get("isbn_13")]
    return cand[0]["id"]


def test_detail_page_renders():
    with _client() as client:
        cid = _save(
            client,
            title="Detail Page Comic",
            issue_number="1",
            isbn_13="9781000000001",
            series="DPC Series",
            publisher="DPC Pub",
        )
        r = client.get(f"/comic/{cid}")
        assert r.status_code == 200
        assert "Detail Page Comic" in r.text
        assert "DPC Series" in r.text
        assert "DPC Pub" in r.text
        assert "COPIES" in r.text


def test_edit_meta_updates_fields():
    with _client() as client:
        cid = _save(
            client,
            title="Original Title",
            isbn_13="9781000000002",
            series="S2",
            publisher="P2",
        )
        r = client.post(
            f"/comic/{cid}/edit",
            data={
                "title": "Renamed Title",
                "issue_number": "42",
                "page_count": "120",
                "isbn_13": "9781000000002",
            },
        )
        assert r.status_code == 200
        assert "Renamed Title" in r.text
        assert "#42" in r.text

        body = client.get(f"/comic/{cid}").text
        assert "Renamed Title" in body and "120" in body


def test_add_and_delete_copy():
    with _client() as client:
        cid = _save(
            client, title="Copy CRUD",
            isbn_13="9781000000003", series="S3", publisher="P3",
        )

        r = client.post(
            f"/comic/{cid}/copies",
            data={"condition": "near-mint-pristine", "storage_location": "Box A", "price_paid_eur": "12.50"},
        )
        assert r.status_code == 200
        assert "near-mint-pristine" in r.text and "Box A" in r.text

        # Add-save also created a copy; delete the most recent one (the NM one) by
        # taking the highest copy id parsed out of the rendered detail page.
        page = client.get(f"/comic/{cid}").text
        import re

        ids = [int(m) for m in re.findall(rf"/comic/{cid}/copies/(\d+)/edit", page)]
        assert ids, "expected at least one copy edit form"
        copy_id = max(ids)

        r = client.post(f"/comic/{cid}/copies/{copy_id}/delete")
        assert r.status_code == 200
        assert "near-mint-pristine" not in r.text


def test_edit_copy_updates_read_status():
    with _client() as client:
        cid = _save(client, title="Read State", isbn_13="9781000000004", series="S4", publisher="P4")
        client.post(f"/comic/{cid}/copies", data={"condition": "VF"})
        page = client.get(f"/comic/{cid}").text
        import re

        copy_id = max(int(m) for m in re.findall(rf"/comic/{cid}/copies/(\d+)/edit", page))

        r = client.post(
            f"/comic/{cid}/copies/{copy_id}/edit",
            data={"read_status": "read", "date_read": "2024-06-01"},
        )
        assert r.status_code == 200
        assert "read" in r.text


def test_delete_comic_redirects_via_htmx_header():
    with _client() as client:
        cid = _save(client, title="Doomed", isbn_13="9781000000005", series="S5", publisher="P5")
        r = client.post(f"/comic/{cid}/delete")
        assert r.status_code == 204
        assert r.headers.get("hx-redirect") == "/library"

        r = client.get(f"/comic/{cid}")
        assert r.status_code == 404
