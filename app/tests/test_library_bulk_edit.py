"""Bulk edit on /library.

POST /library/bulk takes comic_id[] + a set of fields and applies them to
every selected comic in one transaction. Empty fields are no-ops.
"""

from __future__ import annotations

import asyncio
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, Copy


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "Test Publisher", "series": "Test Series"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


def _comic(comic_id: int) -> Comic:
    async def _go():
        async with SessionLocal() as session:
            return await session.get(Comic, comic_id)
    return asyncio.run(_go())


def _first_copy(comic_id: int) -> Copy | None:
    async def _go():
        async with SessionLocal() as session:
            return (await session.exec(
                select(Copy).where(Copy.comic_id == comic_id).order_by(Copy.id.asc()).limit(1)
            )).first()
    return asyncio.run(_go())


def test_bulk_edit_writes_format_canon_era_to_every_selection():
    with _client() as client:
        a = _save(client, title="BE A", isbn_13="9789000000001", series="BE Series A")
        b = _save(client, title="BE B", isbn_13="9789000000002", series="BE Series B")
        c = _save(client, title="BE C", isbn_13="9789000000003", series="BE Series C")

        r = client.post("/library/bulk", data={
            "comic_id": [a, b],  # not c
            "format": "trade paperback",
            "canon": "canon",
            "era": "Imperial",
        }, follow_redirects=False)
        assert r.status_code == 303

        ca, cb, cc = _comic(a), _comic(b), _comic(c)
        assert ca.format == "trade paperback" and cb.format == "trade paperback"
        assert cc.format != "trade paperback"
        assert ca.canon == "canon" and cb.canon == "canon"
        assert ca.era == "Imperial" and cb.era == "Imperial"


def test_bulk_edit_blank_fields_are_no_op():
    with _client() as client:
        cid = _save(client, title="BE Blank", isbn_13="9789000000099",
                    series="BE Blank Series")
        # Pre-set a value so we can detect overwrite.
        async def _seed():
            async with SessionLocal() as session:
                comic = await session.get(Comic, cid)
                comic.format = "single issue"
                session.add(comic)
                await session.commit()
        asyncio.run(_seed())

        r = client.post("/library/bulk", data={
            "comic_id": [cid],
            "format": "",  # blank → keep
            "canon": "",
            "era": "",
        }, follow_redirects=False)
        assert r.status_code == 303
        assert _comic(cid).format == "single issue"


def test_bulk_edit_storage_writes_to_every_copy():
    with _client() as client:
        cid = _save(client, title="BE Stor", isbn_13="9789000000101",
                    series="BE Stor Series")
        # Add a second copy so we can verify all-copies behaviour.
        client.post(f"/comic/{cid}/copies", data={"storage_location": ""})

        client.post("/library/bulk", data={
            "comic_id": [cid],
            "storage_location": "Long Box 9",
        }, follow_redirects=False)

        async def _all_copies():
            async with SessionLocal() as session:
                return (await session.exec(
                    select(Copy).where(Copy.comic_id == cid)
                )).all()
        copies = asyncio.run(_all_copies())
        assert len(copies) >= 2
        assert all(cp.storage_location == "Long Box 9" for cp in copies)


def test_bulk_edit_mark_read_flips_first_unread_copy():
    with _client() as client:
        cid = _save(client, title="BE Read", isbn_13="9789000000201",
                    series="BE Read Series")

        client.post("/library/bulk", data={
            "comic_id": [cid],
            "mark_read": "on",
        }, follow_redirects=False)

        cp = _first_copy(cid)
        assert cp.read_status == "read"
        assert cp.date_read == date.today()


def test_bulk_edit_with_no_selection_redirects_quietly():
    with _client() as client:
        r = client.post("/library/bulk", data={"format": "single issue"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/library"


def test_bulk_edit_redirects_back_to_return_to_url():
    with _client() as client:
        cid = _save(client, title="BE RT", isbn_13="9789000000301",
                    series="BE RT Series")
        r = client.post("/library/bulk", data={
            "comic_id": [cid],
            "return_to": "/library?canon=canon&page=2",
            "canon": "canon",
        }, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/library?canon=canon&page=2"


def _tags_for(comic_id: int) -> set[str]:
    from app.models import ComicTag, Tag
    async def _go():
        async with SessionLocal() as session:
            rows = (await session.exec(
                select(Tag.name).join(ComicTag, ComicTag.tag_id == Tag.id)
                .where(ComicTag.comic_id == comic_id)
            )).all()
            return set(rows)
    return asyncio.run(_go())


def test_bulk_edit_adds_tags_to_every_selection():
    with _client() as client:
        a = _save(client, title="BT A", isbn_13="9789000000501",
                  series="BT Series A")
        b = _save(client, title="BT B", isbn_13="9789000000502",
                  series="BT Series B")
        client.post("/library/bulk", data={
            "comic_id": [a, b],
            "add_tags": "favorite, must-read",
        }, follow_redirects=False)
        assert "favorite" in _tags_for(a)
        assert "must-read" in _tags_for(a)
        assert "favorite" in _tags_for(b)


def test_bulk_edit_removes_tags():
    with _client() as client:
        cid = _save(client, title="BT R", isbn_13="9789000000601",
                    series="BT R Series")
        client.post(f"/comic/{cid}/tags", data={"name": "ditch-me"})
        client.post(f"/comic/{cid}/tags", data={"name": "keep-me"})

        client.post("/library/bulk", data={
            "comic_id": [cid],
            "remove_tags": "ditch-me",
        }, follow_redirects=False)
        names = _tags_for(cid)
        assert "ditch-me" not in names
        assert "keep-me" in names


def test_bulk_edit_add_tags_idempotent():
    with _client() as client:
        cid = _save(client, title="BT I", isbn_13="9789000000701",
                    series="BT I Series")
        client.post("/library/bulk", data={
            "comic_id": [cid],
            "add_tags": "alpha",
        }, follow_redirects=False)
        client.post("/library/bulk", data={
            "comic_id": [cid],
            "add_tags": "alpha",
        }, follow_redirects=False)
        # Tag still appears exactly once via the comic's tag set.
        assert "alpha" in _tags_for(cid)


def test_library_page_includes_bulk_bar_markup():
    with _client() as client:
        _save(client, title="Bar Probe", isbn_13="9789000000401",
              series="Bar Series")
        r = client.get("/library")
        assert r.status_code == 200
        assert 'id="lb-bulk-bar"' in r.text
        assert 'lb-bulk-check' in r.text
        assert 'window.lbBulk' in r.text
