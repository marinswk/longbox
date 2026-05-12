"""Library arc facet (Tier-1 finishing item).

Story arcs come into the system through the refresh-from-source flow,
but for the test we attach them directly to keep the fixtures small.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, ComicArc, StoryArc


def _client() -> TestClient:
    return TestClient(create_app())


async def _attach_arc(comic_title: str, arc_name: str) -> None:
    async with SessionLocal() as session:
        comic = (await session.exec(select(Comic).where(Comic.title == comic_title))).first()
        arc = (await session.exec(select(StoryArc).where(StoryArc.name == arc_name))).first()
        if arc is None:
            arc = StoryArc(name=arc_name)
            session.add(arc)
            await session.flush()
        existing = (
            await session.exec(
                select(ComicArc).where(
                    ComicArc.comic_id == comic.id, ComicArc.arc_id == arc.id
                )
            )
        ).first()
        if existing is None:
            session.add(ComicArc(comic_id=comic.id, arc_id=arc.id))
        await session.commit()


def _seed(client: TestClient, title: str) -> None:
    r = client.post(
        "/add/save",
        data={"title": title, "issue_number": "1", "publisher": "ArcPub", "series": title},
    )
    assert r.status_code == 200


def test_library_filters_by_arc():
    with _client() as client:
        _seed(client, "Arc Comic A")
        _seed(client, "Arc Comic B")
        _seed(client, "Arc Comic C")
        asyncio.run(_attach_arc("Arc Comic A", "War of the Test"))
        asyncio.run(_attach_arc("Arc Comic B", "War of the Test"))

        # Sanity: all three are in the unfiltered library.
        r = client.get("/library")
        assert "Arc Comic A" in r.text
        assert "Arc Comic B" in r.text
        assert "Arc Comic C" in r.text

        # Filtering on the arc keeps A + B, drops C.
        r = client.get("/library", params={"arc": "War of the Test"})
        assert "Arc Comic A" in r.text
        assert "Arc Comic B" in r.text
        assert "Arc Comic C" not in r.text


def test_arc_dropdown_renders_when_arcs_exist():
    with _client() as client:
        _seed(client, "Arc Comic A")
        asyncio.run(_attach_arc("Arc Comic A", "War of the Test"))

        r = client.get("/library")
        assert "Story arc" in r.text
        assert 'name="arc"' in r.text
        assert "All arcs" in r.text
        assert "War of the Test" in r.text


