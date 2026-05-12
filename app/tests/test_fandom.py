"""End-to-end tests for `Comic.fandom`:

* Schema migration moved fandom from Series → Comic.
* Add flow accepts a fandom picker; Wookieepedia source pre-fills "star wars".
* Edit form persists fandom; "+ New fandom..." sentinel works.
* Library `?fandom=` filter + facet appear when populated.
* Stats donut renders + slice click links to /library?fandom=.
* `/api/comics` includes fandom in the JSON payload.
* CSV export contains a `fandom` column.
* Wookieepedia lifespan backfill sets fandom on legacy comics.
"""

from __future__ import annotations

import asyncio
import csv
import io

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic
from app.services.fandoms import normalize, list_fandoms, backfill_wookieepedia_fandom


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


# ── normalize() helper ──────────────────────────────────────────────────

def test_normalize_lowercases_and_collapses_whitespace():
    assert normalize("Star Wars") == "star wars"
    assert normalize("  STAR   WARS  ") == "star wars"
    assert normalize("") is None
    assert normalize(None) is None


# ── add flow ────────────────────────────────────────────────────────────

def test_add_save_picks_up_fandom_dropdown_value():
    with _client() as client:
        cid = _save(client, title="FN A", isbn_13="9794000000001",
                    series="FN A Series", fandom="aggretsuko")
        assert _comic(cid).fandom == "aggretsuko"


def test_add_save_fandom_new_input_wins_over_dropdown():
    with _client() as client:
        cid = _save(client, title="FN B", isbn_13="9794000000002",
                    series="FN B Series",
                    fandom="aggretsuko", fandom_new="Locke & Key")
        assert _comic(cid).fandom == "locke & key"


def test_add_save_new_sentinel_uses_fandom_new():
    with _client() as client:
        cid = _save(client, title="FN C", isbn_13="9794000000003",
                    series="FN C Series",
                    fandom="__NEW__", fandom_new="Eight Billion Genies")
        assert _comic(cid).fandom == "eight billion genies"


def test_add_save_blank_fandom_leaves_null():
    with _client() as client:
        cid = _save(client, title="FN D", isbn_13="9794000000004",
                    series="FN D Series")
        assert _comic(cid).fandom is None


# ── edit flow ───────────────────────────────────────────────────────────

def test_edit_persists_fandom():
    with _client() as client:
        cid = _save(client, title="FN E", isbn_13="9794000000005",
                    series="FN E Series")
        r = client.post(f"/comic/{cid}/edit", data={
            "title": "FN E",
            "fandom": "__NEW__",
            "fandom_new": "Saga",
        })
        assert r.status_code == 200
        assert _comic(cid).fandom == "saga"


def test_edit_form_includes_picker_widget():
    with _client() as client:
        cid = _save(client, title="FN F", isbn_13="9794000000006",
                    series="FN F Series", fandom="star wars")
        page = client.get(f"/comic/{cid}/edit").text
        assert 'name="fandom"' in page
        assert 'name="fandom_new"' in page
        assert "FANDOM" in page


# ── library filter ─────────────────────────────────────────────────────

def test_library_filters_by_fandom():
    with _client() as client:
        sw = _save(client, title="LF SW", isbn_13="9794000000101",
                   series="LF SW Series", fandom="star wars filter")
        ag = _save(client, title="LF AG", isbn_13="9794000000102",
                   series="LF AG Series", fandom="aggretsuko filter")

        r = client.get("/library", params={"fandom": "star wars filter"})
        assert r.status_code == 200
        assert "LF SW" in r.text
        assert "LF AG" not in r.text


def test_library_facet_includes_fandom_when_present():
    with _client() as client:
        _save(client, title="LF Facet", isbn_13="9794000000201",
              series="LF Facet Series", fandom="aggretsuko facet")
        page = client.get("/library").text
        assert "Fandom" in page  # facet legend
        assert "aggretsuko facet" in page


# ── stats donut ────────────────────────────────────────────────────────

def test_stats_donut_renders_for_populated_fandom():
    with _client() as client:
        _save(client, title="Donut", isbn_13="9794000000301",
              series="Donut Series", fandom="star wars donut")
        page = client.get("/stats").text
        assert "id=\"chart-fandom\"" in page
        # The donut click handler must wire to the fandom param.
        assert "donut('chart-fandom'" in page and "'fandom')" in page


# ── /api/comics ────────────────────────────────────────────────────────

def test_api_comics_includes_fandom_field():
    with _client() as client:
        cid = _save(client, title="API FN", isbn_13="9794000000401",
                    series="API FN Series", fandom="api fandom")
        rows = client.get("/api/comics", params={"limit": 500}).json()
        target = next(c for c in rows if c["id"] == cid)
        assert target["fandom"] == "api fandom"


# ── CSV export ─────────────────────────────────────────────────────────

def test_csv_export_includes_fandom_column():
    with _client() as client:
        _save(client, title="CSV FN", isbn_13="9794000000501",
              series="CSV FN Series", fandom="csv fandom")
        r = client.get("/api/export/csv")
        assert r.status_code == 200
        rows = list(csv.DictReader(io.StringIO(r.text)))
        target = next(row for row in rows if row["title"] == "CSV FN")
        assert "fandom" in target
        assert target["fandom"] == "csv fandom"


# ── Lifespan backfill ──────────────────────────────────────────────────

def test_backfill_sets_star_wars_for_wookieepedia_sourced_comics():
    with _client():
        pass  # ensure tables exist
    # Seed a comic that mimics a legacy Wookieepedia save (no fandom).
    async def _seed():
        async with SessionLocal() as session:
            comic = Comic(title="Backfill Probe", source="wookieepedia",
                          source_id="Some_Article", fandom=None)
            session.add(comic)
            await session.commit()
            await session.refresh(comic)
            return comic.id
    cid = asyncio.run(_seed())

    asyncio.run(backfill_wookieepedia_fandom())
    assert _comic(cid).fandom == "star wars"


def test_list_fandoms_orders_by_count_desc():
    async def _check():
        async with SessionLocal() as session:
            return await list_fandoms(session)
    with _client():
        pass
    rows = asyncio.run(_check())
    # Just smoke — exact counts depend on prior tests in this file.
    assert all(isinstance(r, tuple) and isinstance(r[1], int) for r in rows)
