"""Manual re-pick flow at /comic/{id}/repick."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, Series
from app.services.aggregator import LookupResult
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


def _comic(comic_id: int) -> Comic:
    async def _go():
        async with SessionLocal() as session:
            return await session.get(Comic, comic_id)
    return asyncio.run(_go())


def _series_by_name(name: str) -> Optional[Series]:
    async def _go():
        async with SessionLocal() as session:
            return (await session.exec(
                select(Series).where(Series.name == name)
            )).first()
    return asyncio.run(_go())


# ── GET /repick ────────────────────────────────────────────────────────


def test_repick_page_renders_with_seeded_candidates(monkeypatch):
    async def fake_find(**kw):
        return LookupResult(candidates=[
            LookupCandidate(
                source="comicvine", source_id="42",
                title="Auto Hit", series="Auto Series",
            ),
        ])
    monkeypatch.setattr(
        "app.services.aggregator.find_candidates_multi", fake_find,
    )

    with _client() as client:
        cid = _save(client, title="RP Probe", isbn_13="9799200000001",
                    series="RP Series")
        r = client.get(f"/comic/{cid}/repick")
        assert r.status_code == 200
        assert "RE-PICK SOURCE MATCH" in r.text
        assert "Auto Hit" in r.text


def test_repick_page_404_for_unknown_comic():
    with _client() as client:
        r = client.get("/comic/9999999/repick")
        assert r.status_code == 404


# ── POST /repick/search ───────────────────────────────────────────────


def test_repick_search_posts_freeform_query_to_aggregator(monkeypatch):
    captured: list[dict] = []

    async def fake_find(**kw):
        captured.append(kw)
        return LookupResult(candidates=[
            LookupCandidate(source="comicvine", source_id="1",
                            title="Custom Hit", series="Custom Series"),
        ])
    monkeypatch.setattr(
        "app.services.aggregator.find_candidates_multi", fake_find,
    )

    with _client() as client:
        cid = _save(client, title="RP Search", isbn_13="9799200000002",
                    series="RP Search Series")
        r = client.post(
            f"/comic/{cid}/repick/search",
            data={"q": "the real article", "source[comicvine]": "on"},
        )
        assert r.status_code == 200
        assert "Custom Hit" in r.text
        assert captured[-1]["custom_query"] == "the real article"
        assert captured[-1]["sources"] == ["comicvine"]


# ── POST /repick/apply ────────────────────────────────────────────────


def test_repick_apply_swaps_source_and_force_refreshes_metadata(monkeypatch):
    """Picking a different candidate must overwrite source-owned fields
    even when they were already populated."""
    new_candidate = LookupCandidate(
        source="wookieepedia", source_id="Star_Wars_The_High_Republic_Vol_1",
        title="Star Wars: The High Republic Vol. 1",
        series="Star Wars: The High Republic", publisher="Marvel",
        cover_url="http://example.com/tpb.jpg",
        format="trade paperback",
        canon="canon",
        description="A trade paperback collecting issues 1-5.",
    )

    async def fake_refetch(source: str, source_id: str):
        if source == "wookieepedia" and source_id == new_candidate.source_id:
            return new_candidate
        return None
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    with _client() as client:
        cid = _save(client, title="RP Old Title", isbn_13="9799200000101",
                    series="RP Old Series", publisher="RP Pub")
        # Seed an existing source link that doesn't match the new candidate
        # so re-pick clearly transitions away from it.
        async def _seed_source():
            async with SessionLocal() as session:
                c = await session.get(Comic, cid)
                c.source = "wookieepedia"
                c.source_id = "Old_Article"
                c.format = "single issue"
                c.description = "Old description"
                session.add(c)
                await session.commit()
        asyncio.run(_seed_source())

        r = client.post(
            f"/comic/{cid}/repick/apply",
            data={"source": "wookieepedia",
                  "source_id": "Star_Wars_The_High_Republic_Vol_1"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].startswith(f"/comic/{cid}?flash=")

        c = _comic(cid)
        assert c.source == "wookieepedia"
        assert c.source_id == "Star_Wars_The_High_Republic_Vol_1"
        assert c.title == "Star Wars: The High Republic Vol. 1"
        assert c.format == "trade paperback"      # force-overwritten
        assert "trade paperback" == c.format      # normalized lowercase
        assert "collecting issues" in (c.description or "").lower()  # forced replace
        assert c.cover_url_remote == "http://example.com/tpb.jpg"


def test_repick_apply_reassigns_series_and_prunes_orphan(monkeypatch):
    """When the new candidate names a different series, the comic moves;
    the old series is auto-pruned if it just lost its last comic."""
    new_candidate = LookupCandidate(
        source="wookieepedia", source_id="THR_Vol_1",
        title="The High Republic Vol. 1",
        series="Star Wars: The High Republic Volumes",  # different from old
        publisher="Marvel",
    )

    async def fake_refetch(source, source_id):
        return new_candidate
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    with _client() as client:
        cid = _save(client, title="RP MoveMe", isbn_13="9799200000201",
                    series="RP Soon-To-Be-Orphan Series", publisher="RP Pub")
        old_series = _series_by_name("RP Soon-To-Be-Orphan Series")
        assert old_series is not None
        old_id = old_series.id

        client.post(
            f"/comic/{cid}/repick/apply",
            data={"source": "wookieepedia", "source_id": "THR_Vol_1"},
            follow_redirects=False,
        )

        # Comic moved to the new series.
        c = _comic(cid)
        new_series = _series_by_name("Star Wars: The High Republic Volumes")
        assert new_series is not None
        assert c.series_id == new_series.id

        # Old series was pruned because it was empty.
        async def _check_old_gone():
            async with SessionLocal() as session:
                return await session.get(Series, old_id)
        assert asyncio.run(_check_old_gone()) is None


def test_repick_apply_reports_error_when_refetch_returns_none(monkeypatch):
    async def fake_refetch(source, source_id):
        return None
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    with _client() as client:
        cid = _save(client, title="RP Err", isbn_13="9799200000301",
                    series="RP Err Series")
        r = client.post(
            f"/comic/{cid}/repick/apply",
            data={"source": "wookieepedia", "source_id": "missing"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        # Flash carries the error message back to the detail page.
        assert "Couldn" in r.headers["location"]


def test_repick_clears_local_cover_so_detail_page_shows_new_remote(monkeypatch):
    """Regression: after re-pick the detail page used to keep showing
    the OLD cover until the user manually refreshed — the new remote
    URL was set but `cover_url_local` still pointed at the previously
    downloaded file. Re-pick now drops the local pointer so the detail
    template (which prefers local-or-remote) falls back to the new
    remote URL immediately."""
    new_candidate = LookupCandidate(
        source="wookieepedia", source_id="Cover_Article",
        title="Cover Probe",
        series="Cover Series",
        cover_url="http://example.com/new-cover.jpg",
    )

    async def fake_refetch(source, source_id):
        return new_candidate
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    with _client() as client:
        cid = _save(client, title="Cover Old", isbn_13="9799600000001",
                    series="Cover Old Series")
        # Pre-seed an old local cover URL.
        async def _seed():
            async with SessionLocal() as session:
                c = await session.get(Comic, cid)
                c.cover_url_local = "/covers/old-hash.webp"
                c.cover_url_remote = "http://example.com/old.jpg"
                session.add(c)
                await session.commit()
        asyncio.run(_seed())

        # Apply the re-pick. This runs apply_repick + queues a background
        # cover download. The local URL must be wiped synchronously so
        # the next page render falls back to the remote URL.
        client.post(
            f"/comic/{cid}/repick/apply",
            data={"source": "wookieepedia", "source_id": "Cover_Article"},
            follow_redirects=False,
        )
        c = _comic(cid)
        assert c.cover_url_local is None
        assert c.cover_url_remote == "http://example.com/new-cover.jpg"

        # The detail page now renders the new remote URL — no manual
        # refresh required.
        page = client.get(f"/comic/{cid}").text
        assert "http://example.com/new-cover.jpg" in page
        assert "/covers/old-hash.webp" not in page


def test_repick_button_visible_on_detail_page():
    with _client() as client:
        cid = _save(client, title="RP Btn", isbn_13="9799200000401",
                    series="RP Btn Series")
        r = client.get(f"/comic/{cid}")
        assert f'href="/comic/{cid}/repick"' in r.text
