"""Phase 12: full export -> import roundtrip preserves the library."""

from __future__ import annotations

import io
import json

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def _seed(client: TestClient) -> int:
    """Save one comic via the normal /add flow, then attach a tag and a
    second copy. Returns the comic id."""
    r = client.post(
        "/add/save",
        data={
            "title": "Roundtrip Test #1",
            "issue_number": "1",
            "publisher": "Test Publisher",
            "series": "Roundtrip Series",
            "isbn_13": "9780000000017",
            "price_paid_eur": "4.99",
        },
    )
    assert r.status_code == 200

    comics = client.get("/api/comics", params={"limit": 500}).json()
    cid = next(c["id"] for c in comics if c["title"] == "Roundtrip Test #1")

    # second copy
    r = client.post(
        f"/comic/{cid}/copies",
        data={"condition": "VF", "storage_location": "Shelf A"},
    )
    assert r.status_code == 200

    # tag
    r = client.post(f"/comic/{cid}/tags", data={"name": "roundtrip"})
    assert r.status_code == 200
    return cid


def test_export_returns_versioned_payload_with_seeded_data():
    with _client() as client:
        cid = _seed(client)

        r = client.get("/api/export")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert "longbox-backup-" in r.headers["content-disposition"]

        payload = r.json()
        # v2: dropped wishlist/pull_list. v3: moved fandom Series→Comic.
        assert payload["version"] == 3
        assert "exported_at" in payload

        titles = [c["title"] for c in payload["comics"]]
        assert "Roundtrip Test #1" in titles
        # Two copies: the auto one from /add/save + the explicit one above.
        assert sum(1 for cp in payload["copies"] if cp["comic_id"] == cid) == 2
        assert any(t["name"] == "roundtrip" for t in payload["tags"])
        assert any(p["name"] == "Test Publisher" for p in payload["publishers"])


def test_import_round_trip_replaces_library():
    with _client() as client:
        cid = _seed(client)
        before = client.get("/api/export").json()

        # Sanity: collection is non-empty.
        assert len(before["comics"]) >= 1

        # Add a second, *different* comic that the import should wipe.
        r = client.post(
            "/add/save",
            data={
                "title": "Will Be Wiped",
                "issue_number": "99",
                "publisher": "Wipeable",
                "series": "Wipeable",
                "isbn_13": "9780000099999",
            },
        )
        assert r.status_code == 200
        intermediate = client.get("/api/comics", params={"limit": 500}).json()
        assert any(c["title"] == "Will Be Wiped" for c in intermediate)

        # Re-import the earlier export → wiped comic should disappear.
        buf = io.BytesIO(json.dumps(before).encode("utf-8"))
        r = client.post(
            "/admin/import",
            files={"backup": ("backup.json", buf, "application/json")},
        )
        assert r.status_code == 200
        assert "Imported" in r.text

        after = client.get("/api/comics", params={"limit": 500}).json()
        titles = [c["title"] for c in after]
        assert "Roundtrip Test #1" in titles
        assert "Will Be Wiped" not in titles


def test_import_rejects_unknown_version():
    with _client() as client:
        buf = io.BytesIO(b'{"version": 999, "comics": []}')
        r = client.post(
            "/admin/import",
            files={"backup": ("bad.json", buf, "application/json")},
        )
        assert r.status_code == 400
        assert "version" in r.text.lower()


def test_import_rejects_invalid_json():
    with _client() as client:
        buf = io.BytesIO(b"not even json")
        r = client.post(
            "/admin/import",
            files={"backup": ("bad.json", buf, "application/json")},
        )
        assert r.status_code == 400
