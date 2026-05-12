"""Series dedup, merge tool, orphan pruning."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, Series


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    return next(
        c["id"]
        for c in client.get("/api/comics", params={"limit": 500}).json()
        if c.get("isbn_13") == data.get("isbn_13")
    )


async def _series_id(name: str) -> int:
    async with SessionLocal() as s:
        row = (await s.exec(select(Series).where(Series.name == name))).first()
        assert row is not None, f"series {name!r} not found"
        return row.id


# ---------------------------------------------------------------------------
# Dedup at create-time
# ---------------------------------------------------------------------------


def test_get_or_create_series_dedups_across_publishers():
    """Same series name from two different publishers (e.g. Wookieepedia
    'Marvel Comics' vs Open Library 'Marvel Worldwide, Incorporated')
    should reuse the existing row, not create a second one."""
    with _client() as client:
        _save(client, title="Dedup Pub A #1", series="Dedup Series",
              publisher="Big Pub", isbn_13="9786000111111")
        _save(client, title="Dedup Pub B #1", series="Dedup Series",
              publisher="Big Pub Inc., Limited", isbn_13="9786000111222")

        async def _count():
            async with SessionLocal() as s:
                rows = (await s.exec(
                    select(Series).where(Series.name == "Dedup Series")
                )).all()
                return len(rows)
        assert asyncio.run(_count()) == 1


def test_get_or_create_series_normalizes_dashes_and_case():
    """em-dash vs double-hyphen vs single hyphen should all collapse to
    one row."""
    with _client() as client:
        _save(client, title="Dash A #1",
              series="Foo—Bar Series",
              publisher="P", isbn_13="9786000222111")
        _save(client, title="Dash B #1",
              series="foo--bar series",
              publisher="P", isbn_13="9786000222222")
        _save(client, title="Dash C #1",
              series="Foo - Bar Series",
              publisher="P", isbn_13="9786000222333")

        async def _count():
            async with SessionLocal() as s:
                rows = (await s.exec(select(Series))).all()
                return [s.name for s in rows if "foo" in s.name.lower() and "bar" in s.name.lower()]
        names = asyncio.run(_count())
        assert len(names) == 1, f"expected 1 dash-folded series, got {names}"


# ---------------------------------------------------------------------------
# Merge tool
# ---------------------------------------------------------------------------


def test_series_merge_reassigns_comics_and_deletes_source():
    with _client() as client:
        _save(client, title="Merge A #1", series="Merge Source",
              publisher="P", isbn_13="9786000333111")
        _save(client, title="Merge B #1", series="Merge Target",
              publisher="P", isbn_13="9786000333222")

        src = asyncio.run(_series_id("Merge Source"))
        tgt = asyncio.run(_series_id("Merge Target"))

        r = client.post(f"/series/{src}/merge", data={"target_id": tgt})
        assert r.status_code == 204
        assert r.headers.get("HX-Redirect") == f"/series/{tgt}"

        async def _check():
            async with SessionLocal() as s:
                # Source row gone.
                src_row = await s.get(Series, src)
                assert src_row is None
                # Target now has both comics.
                comics = (await s.exec(
                    select(Comic).where(Comic.series_id == tgt)
                )).all()
                titles = sorted(c.title for c in comics)
                assert titles == ["Merge A #1", "Merge B #1"]
        asyncio.run(_check())


def test_series_merge_rejects_self_merge():
    with _client() as client:
        _save(client, title="Self #1", series="Self Merge",
              publisher="P", isbn_13="9786000444111")
        sid = asyncio.run(_series_id("Self Merge"))
        r = client.post(f"/series/{sid}/merge", data={"target_id": sid})
        assert r.status_code == 400


def test_series_merge_does_not_overwrite_target_metadata():
    """Target's source/source_id/expected_issues must not be clobbered."""
    with _client() as client:
        _save(client, title="Keep A #1", series="Keep Target",
              publisher="P", isbn_13="9786000555111")
        _save(client, title="Loss A #1", series="Loss Source",
              publisher="P", isbn_13="9786000555222")

        async def _set_target_meta(name: str):
            async with SessionLocal() as s:
                row = (await s.exec(select(Series).where(Series.name == name))).first()
                row.source = "wookieepedia"
                row.source_id = "Keep Target Article"
                row.expected_issues = "Keep Target 1\nKeep Target 2"
                s.add(row)
                await s.commit()

        async def _set_source_meta(name: str):
            async with SessionLocal() as s:
                row = (await s.exec(select(Series).where(Series.name == name))).first()
                row.source = "comicvine"
                row.source_id = "999"
                row.expected_issues = "Loss 1\nLoss 2"
                s.add(row)
                await s.commit()

        asyncio.run(_set_target_meta("Keep Target"))
        asyncio.run(_set_source_meta("Loss Source"))

        src = asyncio.run(_series_id("Loss Source"))
        tgt = asyncio.run(_series_id("Keep Target"))
        r = client.post(f"/series/{src}/merge", data={"target_id": tgt})
        assert r.status_code == 204

        async def _check():
            async with SessionLocal() as s:
                row = await s.get(Series, tgt)
                assert row.source == "wookieepedia"  # unchanged
                assert row.source_id == "Keep Target Article"
                assert row.expected_issues.startswith("Keep Target")
        asyncio.run(_check())


# ---------------------------------------------------------------------------
# Orphan auto-prune + admin sweep
# ---------------------------------------------------------------------------


def test_deleting_last_comic_in_series_drops_the_series():
    with _client() as client:
        cid = _save(client, title="Lonely #1", series="Solo Series",
                    publisher="P", isbn_13="9786000666111")
        sid = asyncio.run(_series_id("Solo Series"))

        r = client.post(f"/comic/{cid}/delete")
        assert r.status_code == 204

        async def _check():
            async with SessionLocal() as s:
                ghost = await s.get(Series, sid)
                return ghost
        assert asyncio.run(_check()) is None


def test_admin_cleanup_orphans_drops_zero_comic_series():
    with _client() as client:
        # Plant an orphan series via the merge tool: source has 1 comic,
        # we merge into target — source row deletes, but if we instead
        # plant a manually-orphaned row we can verify the sweep.
        async def _plant_orphan():
            async with SessionLocal() as s:
                row = Series(name="Manually Orphaned Series")
                s.add(row)
                await s.commit()
                await s.refresh(row)
                return row.id
        orphan_id = asyncio.run(_plant_orphan())

        r = client.post("/admin/cleanup-orphans")
        assert r.status_code == 200
        assert "Pruned" in r.text

        async def _check():
            async with SessionLocal() as s:
                return await s.get(Series, orphan_id)
        assert asyncio.run(_check()) is None
