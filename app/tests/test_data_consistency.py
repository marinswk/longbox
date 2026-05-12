"""Three fixes covered together:

1. `backfill_merge_duplicate_series` collapses N rows with the same
   normalized name into one canonical row, reassigns child comics, and
   carries over source / source_id / expected_issues / publisher_id from
   the dupes when the canonical row is empty.

2. `_backfill_metadata(force=True)` overwrites every source-derived
   column (title, issue_number, cover URL, cover date, page count,
   description, format, etc.) — not just the small set the original
   refresh button touched.

3. The comic edit form lets the user change the comic's series + the
   parent series's publisher.
"""

from __future__ import annotations

import asyncio
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, Publisher, Series
from app.services.fandoms import backfill_merge_duplicate_series
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


def _series(series_id: int):
    async def _go():
        async with SessionLocal() as session:
            return await session.get(Series, series_id)
    return asyncio.run(_go())


# ── Series-dedup backfill ──────────────────────────────────────────────


def test_backfill_merges_series_with_same_normalized_name():
    """Mimic the High-Republic case: 3 different series rows, all named
    'BMS Same' (case + spacing variants), each holding a different comic."""
    with _client():
        pass

    async def _seed():
        async with SessionLocal() as session:
            # Two publishers we'll later collapse onto one canonical series.
            pub_a = Publisher(name="BMS Pub A", slug="bms-pub-a")
            pub_b = Publisher(name="BMS Pub B", slug="bms-pub-b")
            session.add_all([pub_a, pub_b])
            await session.flush()

            s1 = Series(name="BMS Same", publisher_id=pub_a.id)
            s2 = Series(name="BMS  Same", publisher_id=pub_b.id)  # extra space
            s3 = Series(name="BMS Same", publisher_id=None,
                        source="wookieepedia", source_id="BMS_Same")
            session.add_all([s1, s2, s3])
            await session.flush()

            # Two comics in s1, one in s2, zero in s3.
            session.add_all([
                Comic(series_id=s1.id, title="BMS A1", isbn_13="9799400000001"),
                Comic(series_id=s1.id, title="BMS A2", isbn_13="9799400000002"),
                Comic(series_id=s2.id, title="BMS B1", isbn_13="9799400000003"),
            ])
            await session.commit()
            return [s1.id, s2.id, s3.id]
    sids = asyncio.run(_seed())

    n = asyncio.run(backfill_merge_duplicate_series())
    assert n >= 2  # at least two of the three got merged

    # All three comics should now point at the canonical row (the one with
    # the most comics — s1).
    async def _check():
        async with SessionLocal() as session:
            comics = (await session.exec(
                select(Comic).where(Comic.title.like("BMS %"))
            )).all()
            return {c.title: c.series_id for c in comics}
    placement = asyncio.run(_check())
    canonical = placement["BMS A1"]
    assert placement["BMS A2"] == canonical
    assert placement["BMS B1"] == canonical

    # Source/source_id from s3 was carried onto the canonical row.
    canon = _series(canonical)
    assert canon.source == "wookieepedia"
    assert canon.source_id == "BMS_Same"

    # The other two series rows are gone.
    others = [sid for sid in sids if sid != canonical]
    for sid in others:
        assert _series(sid) is None

    # Idempotent: a second pass merges nothing.
    again = asyncio.run(backfill_merge_duplicate_series())
    assert again == 0


# ── _backfill_metadata force=True covers the full source-owned set ─────


def test_force_backfill_overwrites_title_cover_description_and_friends(monkeypatch):
    """Refresh button should bring every source-derived column up to
    date, not just the small set the original implementation touched."""
    new_candidate = LookupCandidate(
        source="wookieepedia", source_id="Force_Article",
        title="Force Title",
        issue_number="3",
        series="Force Series",
        publisher="Force Publisher",
        cover_url="http://example.com/force.jpg",
        cover_date="2020-04-15",
        page_count=80,
        description="Force-refreshed description text.",
        format="Hardcover",
    )

    async def fake_refetch(source, source_id):
        if source == "wookieepedia" and source_id == "Force_Article":
            return new_candidate
        return None
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    with _client() as client:
        cid = _save(client, title="Force Old", isbn_13="9799400000101",
                    series="Force Old Series", publisher="Force Old Pub")

        # Pre-seed pre-existing values that refresh should overwrite.
        async def _seed():
            async with SessionLocal() as session:
                c = await session.get(Comic, cid)
                c.source = "wookieepedia"
                c.source_id = "Force_Article"
                c.issue_number = "1"
                c.cover_url_remote = "http://example.com/old.jpg"
                c.cover_date = date(2010, 1, 1)
                c.page_count = 24
                c.description = "old"
                c.format = "single issue"
                session.add(c)
                await session.commit()
        asyncio.run(_seed())

        r = client.post(f"/comic/{cid}/refresh", data={
            "source": "wookieepedia", "source_id": "Force_Article",
        })
        assert r.status_code == 204

        c = _comic(cid)
        assert c.title == "Force Title"
        assert c.issue_number == "3"
        assert c.cover_url_remote == "http://example.com/force.jpg"
        assert c.cover_date == date(2020, 4, 15)
        assert c.page_count == 80
        assert c.description == "Force-refreshed description text."
        assert c.format == "hardcover"  # normalized lowercase


# ── Edit form: publisher + series move ────────────────────────────────


def test_edit_publisher_changes_parent_series_publisher():
    with _client() as client:
        cid = _save(client, title="ED Pub", isbn_13="9799400000201",
                    series="ED Pub Series", publisher="ED Old Pub")
        c = _comic(cid)
        old_series_id = c.series_id

        client.post(f"/comic/{cid}/edit", data={
            "title": "ED Pub",
            "publisher": "ED New Pub",  # change publisher
        })

        # Comic stays in the same series, but the series' publisher changes.
        c = _comic(cid)
        assert c.series_id == old_series_id
        ser = _series(c.series_id)
        assert ser is not None
        async def _pub_name():
            async with SessionLocal() as session:
                return (await session.get(Publisher, ser.publisher_id)).name
        assert asyncio.run(_pub_name()) == "ED New Pub"


def test_edit_series_name_moves_comic_to_different_series_row():
    with _client() as client:
        cid = _save(client, title="ED Move", isbn_13="9799400000301",
                    series="ED Move Old Series", publisher="ED Move Pub")
        c = _comic(cid)
        old_series_id = c.series_id

        client.post(f"/comic/{cid}/edit", data={
            "title": "ED Move",
            "series_name": "ED Move New Series",
        })

        c = _comic(cid)
        assert c.series_id != old_series_id
        ser = _series(c.series_id)
        assert ser.name == "ED Move New Series"


def test_edit_form_includes_publisher_and_series_fields():
    with _client() as client:
        cid = _save(client, title="ED Form", isbn_13="9799400000401",
                    series="ED Form Series", publisher="ED Form Pub")
        page = client.get(f"/comic/{cid}/edit").text
        assert 'name="publisher"' in page
        assert 'name="series_name"' in page
