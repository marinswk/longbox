"""CSV export endpoint — flattened spreadsheet view of the library."""

from __future__ import annotations

import asyncio
import csv
import io

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


def test_csv_export_returns_one_row_per_copy_with_metadata():
    with _client() as client:
        cid = _save(client, title="CSV Comic", isbn_13="9787000000001",
                    series="CSV Series", publisher="CSV Publisher")
        client.post(f"/comic/{cid}/tags", data={"name": "favorite"})
        client.post(f"/comic/{cid}/tags", data={"name": "trade"})

        async def _set_copy():
            async with SessionLocal() as session:
                copy = (await session.exec(
                    select(Copy).where(Copy.comic_id == cid).limit(1)
                )).first()
                copy.read_status = "read"
                copy.storage_location = "Long Box 7"
                session.add(copy)
                await session.commit()
        asyncio.run(_set_copy())

        r = client.get("/api/export/csv")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers.get("content-disposition", "")

        reader = csv.DictReader(io.StringIO(r.text))
        rows = list(reader)
        target = [row for row in rows if row["title"] == "CSV Comic"]
        assert len(target) == 1
        row = target[0]
        assert row["series"] == "CSV Series"
        assert row["publisher"] == "CSV Publisher"
        assert row["isbn_13"] == "9787000000001"
        assert row["read_status"] == "read"
        assert row["storage_location"] == "Long Box 7"
        # Tags joined with ';' alphabetically.
        assert row["tags"] == "favorite;trade"


def test_csv_export_filename_has_csv_suffix():
    with _client() as client:
        r = client.get("/api/export/csv")
        assert r.status_code == 200
        cd = r.headers.get("content-disposition", "")
        assert ".csv" in cd
