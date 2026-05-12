"""Quick-mark-read button on comic detail.

POST /comic/{id}/mark-read flips the first not-yet-read copy to read,
sets date_read=today if missing, and re-renders the copies partial.
"""

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


def _read_status(comic_id: int) -> tuple[str | None, date | None]:
    async def _go():
        async with SessionLocal() as session:
            copy = (await session.exec(
                select(Copy).where(Copy.comic_id == comic_id).order_by(Copy.id.asc()).limit(1)
            )).first()
            return (copy.read_status, copy.date_read) if copy else (None, None)
    return asyncio.run(_go())


def test_mark_read_flips_first_unread_copy_with_today_date():
    with _client() as client:
        cid = _save(client, title="QR #1", isbn_13="9788000000001",
                    series="QR Series")
        before_status, before_date = _read_status(cid)
        assert before_status != "read"

        r = client.post(f"/comic/{cid}/mark-read")
        assert r.status_code == 200

        after_status, after_date = _read_status(cid)
        assert after_status == "read"
        assert after_date == date.today()


def test_mark_read_button_hidden_when_all_copies_read():
    with _client() as client:
        cid = _save(client, title="QR #2", isbn_13="9788000000002",
                    series="QR Series 2")
        # First call marks the only copy as read.
        client.post(f"/comic/{cid}/mark-read")
        # Re-fetch the page; the button should no longer appear.
        page = client.get(f"/comic/{cid}").text
        assert "/mark-read" not in page
