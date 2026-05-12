"""Backup zip endpoint — full roundtrip including cover files.

Covers:
  * /api/backup serves a valid zip with library.json + covers/*.
  * /admin/import accepts a zip and restores both data and cover files.
  * Path-traversal entries inside the zip are rejected at restore time.
  * /api/export still works for the data-only flow.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import settings
from app.main import create_app
from app.services.covers import covers_dir


def _client() -> TestClient:
    return TestClient(create_app())


def _seed(client: TestClient) -> int:
    r = client.post(
        "/add/save",
        data={
            "title": "Backup Sample #1",
            "issue_number": "1",
            "publisher": "BkupPub",
            "series": "Bkup Series",
            "isbn_13": "9784000000001",
        },
    )
    assert r.status_code == 200
    return next(c["id"] for c in client.get("/api/comics").json() if c["title"] == "Backup Sample #1")


def _drop_a_cover_file(name: str = "deadbeef0011223344556677.jpg", body: bytes = b"\xff\xd8\xff\xfake-jpeg") -> Path:
    """Plant a fake cover file so the zip has something to bundle."""
    path = covers_dir() / name
    path.write_bytes(body)
    return path


def test_backup_zip_contains_library_json_and_cover_files():
    with _client() as client:
        _seed(client)
        cover_path = _drop_a_cover_file()

        r = client.get("/api/backup")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/zip")
        assert "longbox-backup-" in r.headers["content-disposition"]
        assert r.content[:4] == b"PK\x03\x04"

        with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
            names = zf.namelist()
            assert "library.json" in names
            assert any(n.startswith("covers/") for n in names)
            assert f"covers/{cover_path.name}" in names

            payload = json.loads(zf.read("library.json"))
            assert payload["version"] >= 2
            titles = [c["title"] for c in payload["comics"]]
            assert "Backup Sample #1" in titles


def test_zip_roundtrip_restores_data_and_covers(tmp_path):
    with _client() as client:
        _seed(client)
        cover_path = _drop_a_cover_file(name="roundtrip-cover.jpg", body=b"original-bytes")

        # Capture a zip while the seeded data exists.
        backup = client.get("/api/backup").content

        # Wipe the cover file and add a sacrificial comic.
        cover_path.unlink()
        client.post("/add/save", data={
            "title": "Will Be Wiped", "issue_number": "9",
            "publisher": "Wipeable", "series": "Wipeable",
            "isbn_13": "9784000099999",
        })

        # Restore from the zip.
        r = client.post(
            "/admin/import",
            files={"backup": ("backup.zip", io.BytesIO(backup), "application/zip")},
        )
        assert r.status_code == 200
        assert "cover files" in r.text.lower()

        # Sacrificial comic gone; seeded data + cover file are back.
        titles = [c["title"] for c in client.get("/api/comics").json()]
        assert "Backup Sample #1" in titles
        assert "Will Be Wiped" not in titles
        assert (covers_dir() / "roundtrip-cover.jpg").exists()
        assert (covers_dir() / "roundtrip-cover.jpg").read_bytes() == b"original-bytes"


def test_zip_with_path_traversal_skips_unsafe_entries():
    """A malicious zip with a `covers/../escaped.txt` entry must NOT
    write outside the covers dir."""
    with _client() as client:
        _seed(client)
        payload = {"version": 2, "exported_at": "2026-01-01T00:00:00+00:00"}
        # All entities present as empty lists so import_all is happy.
        for key in ("publishers", "series", "creators", "characters", "story_arcs",
                    "tags", "comics", "copies", "comic_creators",
                    "comic_characters", "comic_arcs", "comic_tags"):
            payload[key] = []

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("library.json", json.dumps(payload))
            # Path-traversal attempt — must be silently dropped.
            zf.writestr("covers/../escaped.txt", b"pwn")
            # Nested-dir attempt — also dropped.
            zf.writestr("covers/sub/dir/file.jpg", b"nested")
            # A safe filename that should land.
            zf.writestr("covers/legit.jpg", b"legit-bytes")
        buf.seek(0)

        r = client.post(
            "/admin/import",
            files={"backup": ("evil.zip", buf, "application/zip")},
        )
        assert r.status_code == 200

        # The legit one landed.
        assert (covers_dir() / "legit.jpg").exists()
        # No file escaped the covers dir.
        escape_targets = [
            settings.data_dir / "escaped.txt",
            settings.data_dir.parent / "escaped.txt",
            covers_dir().parent / "escaped.txt",
        ]
        for target in escape_targets:
            assert not target.exists(), f"path traversal allowed write to {target}"


def test_import_rejects_zip_without_library_json():
    with _client() as client:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("covers/lonely.jpg", b"orphan")
        buf.seek(0)
        r = client.post(
            "/admin/import",
            files={"backup": ("orphan.zip", buf, "application/zip")},
        )
        assert r.status_code == 400
        assert "library.json" in r.text


def test_export_preview_includes_cover_count():
    with _client() as client:
        _seed(client)
        _drop_a_cover_file(name="preview-cover.jpg")
        r = client.get("/api/export/preview")
        assert r.status_code == 200
        body = r.json()
        assert "cover_files" in body
        assert body["cover_files"] >= 1
