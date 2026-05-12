"""Series library page (`GET /series`)."""

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
    payload = {"title": "X", "publisher": "Test Publisher", "series": "Test Series"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


def _series_id_by_name(name: str) -> int:
    async def _go():
        async with SessionLocal() as session:
            row = (await session.exec(select(Series).where(Series.name == name))).first()
            assert row is not None, f"series {name!r} not found"
            return row.id
    return asyncio.run(_go())


def test_series_index_renders_with_series_card_per_row():
    with _client() as client:
        _save(client, title="SI A", isbn_13="9799100000001",
              series="SI Series A", publisher="SI Pub")
        _save(client, title="SI B", isbn_13="9799100000002",
              series="SI Series B", publisher="SI Pub")

        # Search-filter for our seeded series so prior-test pollution
        # doesn't push them past the page-size cap.
        r = client.get("/series", params={"q": "SI Series"})
        assert r.status_code == 200
        assert "YOUR SERIES" in r.text
        sid_a = _series_id_by_name("SI Series A")
        sid_b = _series_id_by_name("SI Series B")
        assert f'href="/series/{sid_a}"' in r.text
        assert f'href="/series/{sid_b}"' in r.text


def test_series_index_filters_by_search_substring():
    with _client() as client:
        _save(client, title="SI Q1", isbn_13="9799100000101",
              series="Apples and Oranges", publisher="P")
        _save(client, title="SI Q2", isbn_13="9799100000102",
              series="Bananas Forever", publisher="P")

        r = client.get("/series", params={"q": "apples"})
        assert "Apples and Oranges" in r.text
        assert "Bananas Forever" not in r.text


def test_series_index_filters_by_fandom_mode():
    with _client() as client:
        # Two comics in the same series, one with a star wars fandom — that
        # series's mode is "star wars sif".
        _save(client, title="SI F1", isbn_13="9799100000201",
              series="SIF Star Wars Series", publisher="P",
              fandom="star wars sif")
        # A different series with a different fandom.
        _save(client, title="SI F2", isbn_13="9799100000202",
              series="SIF Indie Series", publisher="P",
              fandom="aggretsuko sif")

        r = client.get("/series", params={"fandom": "star wars sif"})
        assert "SIF Star Wars Series" in r.text
        assert "SIF Indie Series" not in r.text


def test_series_index_filters_by_status_untracked():
    with _client() as client:
        _save(client, title="SI U1", isbn_13="9799100000301",
              series="SI Untracked", publisher="P")
        # Mark a different series as having an expected-issue list (so it
        # registers as in_progress) — touch series.expected_issues directly.
        _save(client, title="SI T1", isbn_13="9799100000302",
              series="SI Tracked", publisher="P")

        async def _flag():
            async with SessionLocal() as session:
                row = (await session.exec(
                    select(Series).where(Series.name == "SI Tracked")
                )).first()
                row.expected_issues = "Tracked 1\nTracked 2\nTracked 3"
                session.add(row)
                await session.commit()
        asyncio.run(_flag())

        # Untracked filter shows the untracked series, not the tracked one.
        # Add `q` so the filtered list isn't dominated by prior-test data.
        r = client.get("/series", params={"status": "untracked", "q": "SI Un"})
        assert "SI Untracked" in r.text
        assert "SI Tracked" not in r.text


def test_series_index_sorts_by_count_desc():
    with _client() as client:
        # Series with 2 comics.
        _save(client, title="SI Big1", isbn_13="9799100000401",
              series="SI Big", publisher="P")
        _save(client, title="SI Big2", isbn_13="9799100000402",
              series="SI Big", publisher="P")
        # Series with 1 comic.
        _save(client, title="SI Sm1",  isbn_13="9799100000403",
              series="SI Small", publisher="P")

        r = client.get("/series", params={"sort": "count_desc", "q": "SI "})
        # The bigger series appears before the smaller one in the response body.
        assert r.text.index("SI Big") < r.text.index("SI Small")


def test_series_index_grid_partial_swaps_for_pagination():
    with _client() as client:
        _save(client, title="SI Grid", isbn_13="9799100000501",
              series="SI Grid Series", publisher="P")
        r = client.get("/series/grid")
        assert r.status_code == 200
        # Partial doesn't include the page chrome (header / nav).
        assert "<html" not in r.text


def test_top_nav_includes_series_link():
    with _client() as client:
        r = client.get("/library")
        assert 'href="/series"' in r.text
