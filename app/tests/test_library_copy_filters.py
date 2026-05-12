"""Library URL filters that traverse the Copy table.

format/canon/era/tag/arc are tested elsewhere (Comic-level columns). These
two filters cross into Copy via a subquery — the comic shows up if at least
one of its copies matches.
"""

from __future__ import annotations

import asyncio

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


def _set_copy(comic_id: int, **fields):
    async def _go():
        async with SessionLocal() as session:
            copy = (await session.exec(
                select(Copy).where(Copy.comic_id == comic_id).limit(1)
            )).first()
            if copy is None:
                copy = Copy(comic_id=comic_id)
                session.add(copy)
                await session.flush()
            for k, v in fields.items():
                setattr(copy, k, v)
            session.add(copy)
            await session.commit()
    asyncio.run(_go())


def test_library_filters_by_read_status():
    with _client() as client:
        unread_id = _save(client, title="Unread #1", isbn_13="9786000000001",
                          series="Filt Series A")
        read_id = _save(client, title="Read #1", isbn_13="9786000000002",
                        series="Filt Series B")
        _set_copy(unread_id, read_status="unread")
        _set_copy(read_id, read_status="read")

        # Filter to read only.
        r = client.get("/library", params={"read_status": "read", "q": "Filt"})
        assert r.status_code == 200
        assert "Read #1" in r.text
        assert "Unread #1" not in r.text


def test_library_filters_by_storage_location():
    with _client() as client:
        a = _save(client, title="Box A #1", isbn_13="9786000000101",
                  series="Filt Stor A")
        b = _save(client, title="Box B #1", isbn_13="9786000000102",
                  series="Filt Stor B")
        _set_copy(a, storage_location="Long Box 1")
        _set_copy(b, storage_location="Long Box 2")

        r = client.get("/library", params={"storage": "Long Box 1", "q": "Box"})
        assert r.status_code == 200
        assert "Box A #1" in r.text
        assert "Box B #1" not in r.text


def test_library_facets_include_copy_columns():
    with _client() as client:
        cid = _save(client, title="Facet #1", isbn_13="9786000000201",
                    series="Facet Series")
        _set_copy(cid, read_status="read", storage_location="Cabinet")

        r = client.get("/library")
        assert r.status_code == 200
        # The Read-status / Storage facets only appear when there's
        # at least one populated value, so they should show up here.
        assert "Read status" in r.text
        assert "Storage" in r.text
