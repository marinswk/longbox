"""Comic containment (omnibus → TPB linking).

Three things to test:
  1. Library matches return as buttons in the typeahead.
  2. Adding by existing child_id creates the link + re-renders.
  3. Adding by wookieepedia_title creates a stub Comic (no Copy) and
     links it; the stub is hidden in the default library view but
     visible with `?tracked=on`.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import respx
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, ComicContainment, Copy
from app.services.schemas import LookupCandidate


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "Cont Pub", "series": "Cont Series"}
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


def test_search_returns_library_matches():
    with _client() as client:
        parent = _save(client, title="Cont Omnibus",
                       isbn_13="9789000010001", series="Cont OmniSeries")
        child = _save(client, title="Cont TPB One",
                      isbn_13="9789000010002", series="Cont TPBSeries")

        r = client.get(
            f"/comic/{parent}/contains/search",
            params={"q": "tpb one"},
        )
        assert r.status_code == 200
        # Library match appears, with the child_id hidden field.
        assert f'value="{child}"' in r.text
        assert "Cont TPB One" in r.text
        # The Wookieepedia fallback option is always offered.
        assert "Pull" in r.text
        assert "Wookieepedia" in r.text


def test_add_link_by_existing_child_id():
    with _client() as client:
        parent = _save(client, title="Add ParentX",
                       isbn_13="9789000010101", series="AddSer")
        child = _save(client, title="Add ChildX",
                      isbn_13="9789000010102", series="AddChildSer")

        r = client.post(
            f"/comic/{parent}/contains",
            data={"child_id": str(child)},
        )
        assert r.status_code == 200
        # Re-rendered partial includes the child title.
        assert "Add ChildX" in r.text

        async def _check():
            async with SessionLocal() as session:
                link = (await session.exec(
                    select(ComicContainment)
                    .where(ComicContainment.parent_id == parent)
                    .where(ComicContainment.child_id == child)
                )).first()
                return link
        assert asyncio.run(_check()) is not None


def test_add_link_is_idempotent():
    """Posting the same link twice is a no-op, not a duplicate row."""
    with _client() as client:
        parent = _save(client, title="Idem Parent",
                       isbn_13="9789000010201", series="IdemSer")
        child = _save(client, title="Idem Child",
                      isbn_13="9789000010202", series="IdemChildSer")
        client.post(f"/comic/{parent}/contains", data={"child_id": str(child)})
        client.post(f"/comic/{parent}/contains", data={"child_id": str(child)})

        async def _count():
            async with SessionLocal() as session:
                from sqlalchemy import func as _func
                return (await session.exec(
                    select(_func.count())
                    .select_from(ComicContainment)
                    .where(ComicContainment.parent_id == parent)
                    .where(ComicContainment.child_id == child)
                )).first()
        n = asyncio.run(_count())
        n = n[0] if isinstance(n, tuple) else n
        assert int(n) == 1


def test_add_link_rejects_self_reference():
    with _client() as client:
        parent = _save(client, title="Self Parent",
                       isbn_13="9789000010301", series="SelfSer")
        r = client.post(
            f"/comic/{parent}/contains", data={"child_id": str(parent)},
        )
        assert r.status_code == 422


def test_remove_link_drops_the_row():
    with _client() as client:
        parent = _save(client, title="Rem Parent",
                       isbn_13="9789000010401", series="RemSer")
        child = _save(client, title="Rem Child",
                      isbn_13="9789000010402", series="RemChildSer")
        client.post(f"/comic/{parent}/contains", data={"child_id": str(child)})
        r = client.post(f"/comic/{parent}/contains/{child}/delete")
        assert r.status_code == 200

        async def _check():
            async with SessionLocal() as session:
                return (await session.exec(
                    select(ComicContainment)
                    .where(ComicContainment.parent_id == parent)
                )).first()
        assert asyncio.run(_check()) is None


def test_add_via_wookieepedia_creates_stub_comic():
    """The bigger feature: linking a TPB we don't own. The endpoint
    fetches a Wookieepedia article, creates a stub Comic row (no Copy
    attached), and links it. Stub is hidden in the default library
    but appears when tracked=on."""
    fake_cand = LookupCandidate(
        source="wookieepedia",
        source_id="Star Wars Legends Epic Collection: The Empire Vol. 1",
        title="Star Wars Legends Epic Collection: The Empire Vol. 1",
        series="Star Wars Legends Epic Collection",
        publisher="Marvel Comics",
        cover_url="https://example.invalid/cover.png",
        format="trade paperback",
        canon="legends",
        fandom="star wars",
        isbn_13="9781302999999",
    )

    async def fake(title: str):
        assert "Empire" in title
        return fake_cand

    with patch("app.services.wookieepedia.get_article", side_effect=fake), \
         _client() as client:
        parent = _save(client, title="Stub Parent",
                       isbn_13="9789000010501", series="StubSer")
        r = client.post(
            f"/comic/{parent}/contains",
            data={"wookieepedia_title":
                  "Star Wars Legends Epic Collection: The Empire Vol. 1"},
        )
        assert r.status_code == 200
        # Stub appears in the contains partial.
        assert "Empire Vol. 1" in r.text

        # Stub has zero copies (it's a tracked-only reference).
        async def _stub_copies():
            async with SessionLocal() as session:
                stub = (await session.exec(
                    select(Comic).where(
                        Comic.title == "Star Wars Legends Epic Collection: The Empire Vol. 1"
                    )
                )).first()
                assert stub is not None
                count = (await session.exec(
                    select(Copy).where(Copy.comic_id == stub.id)
                )).all()
                return stub.id, len(count)
        stub_id, copy_count = asyncio.run(_stub_copies())
        assert copy_count == 0

        # Default library hides stubs. Use a distinctive isbn check —
        # the search input value pre-fills with `q=...` and would
        # otherwise produce a false-positive on substring match.
        r = client.get("/library", params={"q": "Empire Vol. 1"})
        assert f'href="/comic/{stub_id}"' not in r.text
        # With ?tracked=on the stub appears as a clickable card.
        r = client.get("/library", params={"q": "Empire Vol. 1", "tracked": "on"})
        assert f'href="/comic/{stub_id}"' in r.text


def test_comic_detail_renders_contains_section():
    with _client() as client:
        cid = _save(client, title="Cnt Render",
                    isbn_13="9789000010601", series="CntSer")
        r = client.get(f"/comic/{cid}")
        assert r.status_code == 200
        assert 'id="contains-section"' in r.text
        assert "CONTAINS" in r.text
        assert "Type a title" in r.text
