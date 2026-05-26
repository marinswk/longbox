"""Factory-reset endpoint at /admin/wipe."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import (
    Character, Comic, ComicArc, ComicCharacter, ComicContainment,
    ComicCreator, ComicSeries, ComicTag, Copy, Creator, ImportRow,
    ImportSession, MetadataCache, Publisher, Series, StoryArc, Tag,
)


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "Test Publisher", "series": "Test Series"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


def _row_count(model) -> int:
    async def _go():
        async with SessionLocal() as session:
            return len((await session.exec(select(model))).all())
    return asyncio.run(_go())


# ── Confirmation gate ──────────────────────────────────────────────────


def test_wipe_rejects_missing_confirmation():
    with _client() as client:
        _save(client, title="Wipe Probe", isbn_13="9799500000001",
              series="Wipe Series", publisher="Wipe Pub")
        before = _row_count(Comic)
        r = client.post("/admin/wipe", data={"confirm": ""})
        assert r.status_code == 400
        assert "didn" in r.text.lower() or "match" in r.text.lower()
        # Nothing got deleted.
        assert _row_count(Comic) == before


def test_wipe_rejects_wrong_confirmation_phrase():
    with _client() as client:
        _save(client, title="Wipe Probe2", isbn_13="9799500000002",
              series="Wipe Series 2", publisher="Wipe Pub")
        before = _row_count(Comic)
        r = client.post("/admin/wipe", data={"confirm": "wipe everything"})
        # Lowercase ≠ uppercase WIPE EVERYTHING.
        assert r.status_code == 400
        assert _row_count(Comic) == before


# ── Happy path: full wipe ──────────────────────────────────────────────


def test_wipe_with_confirmation_clears_every_user_data_table():
    with _client() as client:
        # Seed at least one row in every major table the wipe should clear.
        cid = _save(client, title="WipeMe", isbn_13="9799500000003",
                    series="WipeMe Series", publisher="WipeMe Pub")
        client.post(f"/comic/{cid}/copies",
                    data={"condition": "VF", "storage_location": "Box A"})
        client.post(f"/comic/{cid}/tags", data={"name": "favorite"})

        # Sanity: everything is non-empty.
        assert _row_count(Comic) > 0
        assert _row_count(Copy) > 0
        assert _row_count(Series) > 0
        assert _row_count(Publisher) > 0
        assert _row_count(Tag) > 0
        assert _row_count(ComicTag) > 0

        r = client.post("/admin/wipe", data={
            "confirm": "WIPE EVERYTHING",
            # Cover files are tested separately below — keep them here.
        })
        assert r.status_code == 200
        assert "Wiped" in r.text

        for model in (Comic, Copy, Series, Publisher, Creator, Character,
                      StoryArc, Tag, ComicCreator, ComicCharacter,
                      ComicArc, ComicTag, ComicSeries, ComicContainment,
                      ImportSession, ImportRow, MetadataCache):
            assert _row_count(model) == 0, f"{model.__name__} not empty after wipe"


def test_wipe_keeps_schema_intact_so_app_still_works():
    """After a wipe the user can immediately start adding comics again
    without restarting the container — the schema is unchanged."""
    with _client() as client:
        client.post("/admin/wipe", data={"confirm": "WIPE EVERYTHING"})
        # Add a fresh comic post-wipe.
        cid = _save(client, title="Post-Wipe", isbn_13="9799500000099",
                    series="Post-Wipe Series", publisher="Post-Wipe Pub")
        assert _row_count(Comic) >= 1
        # Detail page renders.
        r = client.get(f"/comic/{cid}")
        assert r.status_code == 200


# ── Cover-file deletion ────────────────────────────────────────────────


def test_wipe_with_delete_cover_files_removes_local_covers(tmp_path, monkeypatch):
    """`delete_cover_files=on` deletes every file under `covers_dir()`."""
    # Redirect covers_dir() to a temp directory we control.
    from app.services import covers as covers_mod
    monkeypatch.setattr(covers_mod, "covers_dir", lambda: tmp_path)

    # Seed a few fake cover files.
    files = [tmp_path / "a.jpg", tmp_path / "b.webp", tmp_path / "c.png"]
    for p in files:
        p.write_bytes(b"fake")

    with _client() as client:
        r = client.post("/admin/wipe", data={
            "confirm": "WIPE EVERYTHING",
            "delete_cover_files": "on",
        })
        assert r.status_code == 200

    # Files are gone, directory is intact.
    for p in files:
        assert not p.exists()
    assert tmp_path.exists()


def test_wipe_without_delete_cover_files_keeps_covers(tmp_path, monkeypatch):
    from app.services import covers as covers_mod
    monkeypatch.setattr(covers_mod, "covers_dir", lambda: tmp_path)

    survivor = tmp_path / "survivor.jpg"
    survivor.write_bytes(b"keep me")

    with _client() as client:
        # delete_cover_files NOT submitted → defaults to off.
        r = client.post("/admin/wipe", data={"confirm": "WIPE EVERYTHING"})
        assert r.status_code == 200

    assert survivor.exists()


# ── UI surface ─────────────────────────────────────────────────────────


def test_admin_page_includes_danger_zone_with_wipe_form():
    with _client() as client:
        r = client.get("/admin")
        assert r.status_code == 200
        assert "DANGER ZONE" in r.text
        assert 'hx-post="/admin/wipe"' in r.text
        assert "WIPE EVERYTHING" in r.text  # confirmation phrase visible
        # Sub-nav anchor present.
        assert 'href="#danger"' in r.text
