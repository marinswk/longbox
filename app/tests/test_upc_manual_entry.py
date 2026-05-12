"""Manual entry preserves the UPC the user scanned, /add/save persists it,
duplicate detection uses it, and the library card surfaces it.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_picker_manual_entry_button_carries_upc():
    """When a UPC lookup returns no candidates, the manual-entry button
    should pre-fill the UPC into a hidden form field so /add/confirm
    sees it."""
    with _client() as client:
        r = client.post("/add/lookup", data={"identifier": "64985600095800999"})
        assert r.status_code == 200
        assert 'name="upc" value="64985600095800999"' in r.text


def test_confirm_renders_upc_input_and_save_persists_it():
    upc = "76194131234500111"
    with _client() as client:
        # /add/confirm with no duplicate → renders the editable form.
        r = client.post(
            "/add/confirm",
            data={"title": "UPC Manual Test", "upc": upc, "source": "manual"},
        )
        assert r.status_code == 200
        # Confirm form should have the UPC input pre-filled.
        assert 'name="upc"' in r.text
        assert f'value="{upc}"' in r.text

        # Save with the UPC populated.
        r = client.post(
            "/add/save",
            data={
                "title": "UPC Manual Test",
                "upc": upc,
                "publisher": "Test Pub",
                "series": "UPC Test Series",
            },
        )
        assert r.status_code == 200
        # Comic landed in the library with upc populated.
        comics = client.get("/api/comics", params={"limit": 500}).json()
        match = [c for c in comics if c.get("title") == "UPC Manual Test"]
        assert len(match) == 1
        assert match[0]["upc"] == upc


def test_duplicate_detection_by_upc():
    upc = "76194131234500222"
    with _client() as client:
        # Save first copy.
        client.post(
            "/add/save",
            data={"title": "Dup UPC Test", "upc": upc,
                  "publisher": "P", "series": "Dup UPC Series"},
        )
        # Confirm with same UPC → duplicate prompt.
        r = client.post("/add/confirm", data={"title": "Dup UPC Test", "upc": upc})
        assert r.status_code == 200
        assert "YOU ALREADY OWN THIS" in r.text


def test_library_card_shows_upc():
    upc = "76194131234500333"
    with _client() as client:
        client.post(
            "/add/save",
            data={"title": "Visible UPC", "upc": upc,
                  "publisher": "P", "series": "Visible UPC Series"},
        )
        r = client.get("/library")
        assert r.status_code == 200
        # The card renders a UPC <p> with the digits in monospace.
        assert "UPC" in r.text
        assert upc in r.text


def test_comic_edit_form_persists_upc_change():
    initial = "76194131234500444"
    new_upc = "76194131234500555"
    with _client() as client:
        client.post(
            "/add/save",
            data={"title": "Edit UPC", "upc": initial,
                  "publisher": "P", "series": "Edit UPC Series"},
        )
        comics = client.get("/api/comics", params={"limit": 500}).json()
        cid = next(c["id"] for c in comics if c["title"] == "Edit UPC")

        # Submit the edit form with a new UPC.
        r = client.post(
            f"/comic/{cid}/edit",
            data={"title": "Edit UPC", "upc": new_upc},
        )
        assert r.status_code == 200
        comic = client.get(f"/api/comics/{cid}").json()
        assert comic["upc"] == new_upc
