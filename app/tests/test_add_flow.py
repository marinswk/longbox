"""End-to-end tests for the HTMX add-comic flow."""

import httpx
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import covers


def _client() -> TestClient:
    return TestClient(create_app())


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x00\x03\x00\x01\x5b\x9b\x05\xa4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_get_add_page_renders():
    with _client() as client:
        r = client.get("/add")
        assert r.status_code == 200
        assert "ADD A COMIC" in r.text
        assert "/add/lookup" in r.text
        # Webcam scanner is wired in.
        assert "html5-qrcode" in r.text
        assert 'id="scan-toggle"' in r.text
        assert 'id="scanner"' in r.text


@respx.mock
def test_lookup_partial_renders_picker():
    isbn = "9780000000111"
    respx.get("https://openlibrary.org/api/books").mock(
        return_value=httpx.Response(
            200,
            json={
                f"ISBN:{isbn}": {
                    "title": "Saga, Volume One",
                    "publishers": [{"name": "Image Comics"}],
                    "publish_date": "2012",
                    "number_of_pages": 160,
                    "cover": {"large": "https://covers.example/saga.jpg"},
                    "key": "/books/OL12345M",
                }
            },
        )
    )
    with _client() as client:
        r = client.post("/add/lookup", data={"identifier": isbn})
        assert r.status_code == 200
        assert "PICK ONE" in r.text
        assert "Saga" in r.text
        assert "/add/confirm" in r.text


def test_confirm_partial_renders_editable_fields():
    with _client() as client:
        r = client.post(
            "/add/confirm",
            data={
                "title": "Saga #1",
                "isbn_13": "9780000000222",
                "cover_url_remote": "https://covers.example/saga.jpg",
                "source": "metron",
            },
        )
        assert r.status_code == 200
        assert "CONFIRM" in r.text
        assert "Saga #1" in r.text
        assert "/add/save" in r.text
        # Visible (not hidden) inputs for the user-editable fields.
        assert 'name="title"' in r.text and 'type="text"' in r.text
        assert 'name="series"' in r.text
        assert 'name="publisher"' in r.text
        assert 'name="issue_number"' in r.text


def test_save_falls_back_to_title_as_series_when_publisher_set():
    """OL gives publisher but no series for trades; we promote title→series
    so the comic is attached to a Publisher row via Series."""
    isbn = "9780000000888"
    with _client() as client:
        r = client.post(
            "/add/save",
            data={
                "title": "Star Wars: Jedi Knights Vol. 1",
                "publisher": "Marvel Worldwide, Incorporated",
                "isbn_13": isbn,
            },
        )
        assert r.status_code == 200
        # Library page should now show the publisher in the facet sidebar.
        body = client.get("/library").text
        assert "Marvel Worldwide, Incorporated" in body
        assert "Star Wars: Jedi Knights Vol. 1" in body


@respx.mock
def test_save_creates_comic_and_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(covers.settings, "data_dir", tmp_path)
    cover_remote = "https://covers.example/saga-save.jpg"
    respx.get(cover_remote).mock(
        return_value=httpx.Response(200, content=PNG, headers={"content-type": "image/png"})
    )
    isbn = "9780000000333"
    with _client() as client:
        r = client.post(
            "/add/save",
            data={
                "title": "Saga #1",
                "isbn_13": isbn,
                "cover_url_remote": cover_remote,
                "price_paid_eur": "9.99",
            },
        )
        assert r.status_code == 200
        assert "POW!" in r.text and "ADDED TO COLLECTION" in r.text
        assert "Saga #1" in r.text

        comics = client.get("/api/comics").json()
        match = [c for c in comics if c["isbn_13"] == isbn]
        assert len(match) == 1
        assert match[0]["title"] == "Saga #1"


def test_save_with_variant_persists_on_first_copy():
    """The /add confirm picker writes the chosen variant into hidden
    fields; /add/save must persist them on the freshly-created Copy
    so the user sees their variant cover + label on /comic/{id}."""
    isbn = "9780000000555"
    with _client() as client:
        r = client.post(
            "/add/save",
            data={
                "title": "VAR Issue 1",
                "isbn_13": isbn,
                "variant_name": "Carbonite variant",
                "variant_cover_url": "https://covers.example/wbh5-carbonite.jpg",
            },
        )
        assert r.status_code == 200

        comics = client.get("/api/comics", params={"limit": 500}).json()
        comic_id = next(c["id"] for c in comics if c["isbn_13"] == isbn)
        # The detail page renders the variant + thumbnail on the copy row.
        page = client.get(f"/comic/{comic_id}").text
        assert "Carbonite variant" in page
        assert "wbh5-carbonite.jpg" in page


def test_add_copy_form_records_variant_from_gallery_index():
    """The add-copy form on /comic/{id} accepts a gallery index in
    `variant_choice` and resolves it against Comic.cover_variants_json
    server-side (no upstream re-fetch)."""
    import json as _json
    isbn = "9780000000556"
    with _client() as client:
        # Seed a comic with a cached gallery on it.
        client.post(
            "/add/save",
            data={"title": "VAR Issue 2", "isbn_13": isbn},
        )
        comics = client.get("/api/comics", params={"limit": 500}).json()
        comic_id = next(c["id"] for c in comics if c["isbn_13"] == isbn)

        # Inject a known cached gallery so the test doesn't depend on
        # upstream variants. Two entries: index 1 = McNiven, 2 = Carbonite.
        import asyncio
        from app.db import SessionLocal
        from app.models import Comic
        async def _seed_gallery():
            async with SessionLocal() as s:
                c = await s.get(Comic, comic_id)
                c.cover_variants_json = _json.dumps([
                    {"label": "McNiven cover", "url": "https://covers.example/mcniven.jpg"},
                    {"label": "Carbonite variant", "url": "https://covers.example/carbonite.jpg"},
                ])
                s.add(c)
                await s.commit()
        asyncio.run(_seed_gallery())

        # Pick variant 2 (Carbonite) via the add-copy form.
        r = client.post(
            f"/comic/{comic_id}/copies",
            data={"variant_choice": "2"},
        )
        assert r.status_code == 200
        # The resulting copies-section partial shows the carbonite label + url.
        assert "Carbonite variant" in r.text
        assert "covers.example/carbonite.jpg" in r.text


def test_add_copy_form_other_variant_uses_free_text():
    """`variant_choice=__other__` records `variant_name_other` as the
    variant label, leaves variant_cover_url NULL — user can edit it later."""
    isbn = "9780000000557"
    with _client() as client:
        client.post("/add/save", data={"title": "VAR Issue 3", "isbn_13": isbn})
        comics = client.get("/api/comics", params={"limit": 500}).json()
        comic_id = next(c["id"] for c in comics if c["isbn_13"] == isbn)

        r = client.post(
            f"/comic/{comic_id}/copies",
            data={"variant_choice": "__other__",
                  "variant_name_other": "SDCC 2025 exclusive"},
        )
        assert r.status_code == 200
        assert "SDCC 2025 exclusive" in r.text


def test_confirm_flags_duplicate_after_save():
    isbn = "9780000000444"
    with _client() as client:
        client.post(
            "/add/save",
            data={"title": "Y the Last Man Vol 1", "isbn_13": isbn},
        )
        r = client.post(
            "/add/confirm",
            data={"title": "Y the Last Man Vol 1", "isbn_13": isbn},
        )
        assert r.status_code == 200
        assert "YOU ALREADY OWN THIS" in r.text
        assert ">1</strong>" in r.text and " copy" in r.text


def test_save_existing_increments_copy_count():
    isbn = "9780000000555"
    with _client() as client:
        client.post("/add/save", data={"title": "X", "isbn_13": isbn})
        comics = client.get("/api/comics").json()
        existing_id = next(c["id"] for c in comics if c["isbn_13"] == isbn)

        r = client.post(
            "/add/save",
            data={
                "existing_comic_id": str(existing_id),
                "title": "X",
                "isbn_13": isbn,
                "price_paid_eur": "5.50",
            },
        )
        assert r.status_code == 200
        assert ">2</strong>" in r.text and " copies" in r.text
