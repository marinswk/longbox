"""Comic edit form covers every Comic column the user might care about.

Audit added: format, language, canon, era, timeline, collected_issues.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic


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


def test_edit_form_renders_all_audited_fields():
    with _client() as client:
        cid = _save(client, title="EF Probe", isbn_13="9792000000001",
                    series="EF Series")
        page = client.get(f"/comic/{cid}/edit").text
        for name in ("format", "language", "canon", "era", "timeline",
                     "collected_issues"):
            assert f'name="{name}"' in page, f"missing form field: {name}"


def test_edit_endpoint_persists_new_fields():
    with _client() as client:
        cid = _save(client, title="EF Save", isbn_13="9792000000002",
                    series="EF Save Series")
        r = client.post(f"/comic/{cid}/edit", data={
            "title": "EF Save",
            "format": "trade paperback",
            "language": "English",
            "canon": "legends",
            "era": "Old Republic",
            "timeline": "3956 BBY",
            "collected_issues": "Knights 1\nKnights 2\nKnights 3",
        })
        assert r.status_code == 200
        c = _comic(cid)
        assert c.format == "trade paperback"
        assert c.language == "English"
        assert c.canon == "legends"
        assert c.era == "Old Republic"
        assert c.timeline == "3956 BBY"
        assert c.collected_issues == "Knights 1\nKnights 2\nKnights 3"
