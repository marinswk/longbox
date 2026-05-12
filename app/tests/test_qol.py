"""Phase 11a — quality-of-life batch:
- manual-entry fallback when lookup returns zero
- manual cover upload on the detail page
- tags add/remove + library tag filter
"""

import io
import re

import httpx
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import covers


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client, **data):
    payload = {"title": "X"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


# ---------------------------------------------------------------------------
# Manual-entry fallback
# ---------------------------------------------------------------------------


@respx.mock
def test_picker_offers_manual_entry_when_no_matches():
    isbn = "9780000111000"
    respx.get("https://starwars.fandom.com/api.php").mock(
        return_value=httpx.Response(200, json={"query": {"search": []}})
    )
    respx.get("https://openlibrary.org/api/books").mock(
        return_value=httpx.Response(200, json={})
    )

    with _client() as client:
        r = client.post("/add/lookup", data={"identifier": isbn})
        assert r.status_code == 200
        assert "No matches" in r.text
        assert "Enter manually" in r.text
        # The hidden form must carry the ISBN through to /add/confirm.
        assert "/add/confirm" in r.text
        assert isbn in r.text


# ---------------------------------------------------------------------------
# Manual cover upload
# ---------------------------------------------------------------------------


PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0"
    b"\x00\x00\x00\x03\x00\x01\x5b\x9b\x05\xa4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_upload_cover_writes_file_and_sets_local_url(tmp_path, monkeypatch):
    monkeypatch.setattr(covers.settings, "data_dir", tmp_path)
    with _client() as client:
        cid = _save(client, title="Upload Test", isbn_13="9780001000111", series="S", publisher="P")
        r = client.post(
            f"/comic/{cid}/cover/upload",
            files={"cover": ("cover.png", io.BytesIO(PNG), "image/png")},
        )
        assert r.status_code == 200

        comics = client.get("/api/comics", params={"limit": 500}).json()
        match = next(c for c in comics if c["id"] == cid)
        assert match["cover_url_local"] is not None
        assert match["cover_url_local"].startswith("/covers/")
        # The on-disk file actually exists.
        path = tmp_path / "covers" / match["cover_url_local"].split("/")[-1]
        assert path.exists() and path.read_bytes() == PNG


def test_upload_cover_rejects_non_image_content_type(tmp_path, monkeypatch):
    monkeypatch.setattr(covers.settings, "data_dir", tmp_path)
    with _client() as client:
        cid = _save(client, title="Upload Test 2", isbn_13="9780001000222", series="S2", publisher="P2")
        r = client.post(
            f"/comic/{cid}/cover/upload",
            files={"cover": ("not-image.txt", io.BytesIO(b"hello"), "text/plain")},
        )
        assert r.status_code == 415


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def test_add_and_remove_tag_round_trip():
    with _client() as client:
        cid = _save(client, title="Tagged", isbn_13="9780001000333", series="S3", publisher="P3")

        r = client.post(f"/comic/{cid}/tags", data={"name": "Star Wars"})
        assert r.status_code == 200
        assert "star wars" in r.text  # normalized to lowercase

        page = client.get(f"/comic/{cid}").text
        assert "star wars" in page

        # Find the tag id from the rendered remove form.
        m = re.search(r'<input type="hidden" name="tag_id" value="(\d+)"', page)
        assert m, "expected at least one remove form"
        tag_id = m.group(1)

        r = client.post(f"/comic/{cid}/tags/remove", data={"tag_id": tag_id})
        assert r.status_code == 200
        assert "No tags yet" in r.text


def test_library_filters_by_tag():
    with _client() as client:
        a = _save(client, title="Tagged A", isbn_13="9780001000444", series="A", publisher="A")
        b = _save(client, title="Untagged B", isbn_13="9780001000555", series="B", publisher="B")
        client.post(f"/comic/{a}/tags", data={"name": "favorite"})
        client.post(f"/comic/{b}/tags", data={"name": "borrowed"})

        # Filter by tag=favorite — should include A but not B.
        r = client.get("/library", params={"tag": "favorite"})
        assert r.status_code == 200
        assert "Tagged A" in r.text
        assert "Untagged B" not in r.text

        # Tag facet appears in the sidebar.
        assert 'name="tag"' in r.text
        assert "favorite" in r.text


def test_tag_normalization_collapses_duplicates():
    with _client() as client:
        cid = _save(client, title="Dedup", isbn_13="9780001000666", series="D", publisher="D")
        client.post(f"/comic/{cid}/tags", data={"name": "  Star  Wars  "})
        client.post(f"/comic/{cid}/tags", data={"name": "STAR WARS"})

        page = client.get(f"/comic/{cid}").text
        # Should be a single normalized "star wars" tag, not two duplicates.
        assert page.count('name="tag_id"') == 1
