"""Auto-tagging on add + retro-fill via POST /comic/{id}/auto-tag.

The save flow already calls `_autotag_from_candidate`. This test exercises
the retro-fill endpoint end-to-end against a stubbed candidate so we don't
depend on a live ComicVine / Metron / Wookieepedia fetch.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, ComicTag, Tag
from app.services.schemas import LookupCandidate


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "Test Publisher", "series": "Test Series"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


def _set_source(comic_id: int, source: str, source_id: str):
    async def _go():
        async with SessionLocal() as session:
            c = await session.get(Comic, comic_id)
            c.source = source
            c.source_id = source_id
            session.add(c)
            await session.commit()
    asyncio.run(_go())


def _tags_for(comic_id: int) -> list[str]:
    async def _go():
        async with SessionLocal() as session:
            rows = (await session.exec(
                select(Tag.name).join(ComicTag, ComicTag.tag_id == Tag.id)
                .where(ComicTag.comic_id == comic_id)
            )).all()
            return list(rows)
    return asyncio.run(_go())


def test_auto_tag_endpoint_applies_chars_and_arcs(monkeypatch):
    """End-to-end auto-tag against a stubbed candidate. Concepts are
    intentionally NOT applied (see test_auto_tag_polish for the rationale)."""
    async def fake_refetch(source: str, source_id: str) -> Optional[LookupCandidate]:
        if not source or not source_id:
            return None
        return LookupCandidate(
            source="comicvine", source_id=source_id,
            characters=["Boba Fett", "Han Solo"],
            story_arcs=["War of the Bounty Hunters"],
        )
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    with _client() as client:
        cid = _save(client, title="AT #1", isbn_13="9791000000001",
                    series="AT Series")
        _set_source(cid, "comicvine", "999999")

        r = client.post(f"/comic/{cid}/auto-tag")
        assert r.status_code == 200

        names = set(_tags_for(cid))
        assert "chars: boba fett" in names
        assert "chars: han solo" in names
        assert "war of the bounty hunters" in names


def test_auto_tag_no_op_when_no_source_set():
    with _client() as client:
        cid = _save(client, title="AT NoSrc", isbn_13="9791000000099",
                    series="AT NoSrc Series")
        before = set(_tags_for(cid))
        r = client.post(f"/comic/{cid}/auto-tag")
        assert r.status_code == 200
        assert set(_tags_for(cid)) == before
