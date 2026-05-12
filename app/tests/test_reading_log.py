"""/reading-log: timeline view of every read copy with date_read set."""

from __future__ import annotations

import asyncio
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Copy


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "Test Publisher", "series": "Test Series"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


def _mark_read(comic_id: int, when: date):
    async def _go():
        async with SessionLocal() as session:
            copy = (await session.exec(
                select(Copy).where(Copy.comic_id == comic_id).limit(1)
            )).first()
            copy.read_status = "read"
            copy.date_read = when
            session.add(copy)
            await session.commit()
    asyncio.run(_go())


def test_reading_log_empty_state_renders():
    with _client() as client:
        r = client.get("/reading-log")
        assert r.status_code == 200
        assert "READING LOG" in r.text


def test_reading_log_groups_reads_by_month_newest_first():
    with _client() as client:
        a = _save(client, title="Old Read", isbn_13="9787100000001",
                  series="RL Series A")
        b = _save(client, title="New Read", isbn_13="9787100000002",
                  series="RL Series B")
        _mark_read(a, date(2024, 6, 15))
        _mark_read(b, date(2025, 3, 1))

        r = client.get("/reading-log")
        assert r.status_code == 200
        assert "Old Read" in r.text
        assert "New Read" in r.text
        # The most recent month appears before the older one in the page.
        idx_new = r.text.index("2025-03")
        idx_old = r.text.index("2024-06")
        assert idx_new < idx_old


def test_unread_copies_do_not_appear_in_log():
    with _client() as client:
        cid = _save(client, title="Never Read", isbn_13="9787100000003",
                    series="RL Series C")
        # No date_read / no read_status flip — should not appear.
        r = client.get("/reading-log")
        assert r.status_code == 200
        assert "Never Read" not in r.text
