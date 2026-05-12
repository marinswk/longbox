"""Liberal-flag sweep at /admin/inconsistencies."""

from __future__ import annotations

import asyncio
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic
from app.services.inconsistencies import find_inconsistencies


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "Test Publisher", "series": "Test Series"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


def _patch(comic_id: int, **fields):
    async def _go():
        async with SessionLocal() as session:
            c = await session.get(Comic, comic_id)
            for k, v in fields.items():
                setattr(c, k, v)
            session.add(c)
            await session.commit()
    asyncio.run(_go())


def _flagged_for(target_ids: list[int]):
    """Return only flags for comics we care about — the test DB is shared."""
    async def _go():
        async with SessionLocal() as session:
            return await find_inconsistencies(session)
    rows = asyncio.run(_go())
    return [f for f in rows if f.comic.id in target_ids]


# ── Heuristic 1: prose_collects ────────────────────────────────────────


def test_flag_prose_collects_when_collected_starts_with_collecting():
    with _client() as client:
        cid = _save(client, title="IC Prose1", isbn_13="9799300000001",
                    series="IC Series Prose1")
        _patch(
            cid,
            collected_issues="COLLECTING: Star Wars (2015) #1-14, Annual 1",
            format="trade paperback",
        )
        flagged = _flagged_for([cid])
        assert flagged, "expected the comic to be flagged"
        codes = {r.code for r in flagged[0].reasons}
        assert "prose_collects" in codes


def test_flag_prose_collects_when_collected_has_commas():
    with _client() as client:
        cid = _save(client, title="IC Prose2", isbn_13="9799300000002",
                    series="IC Series Prose2")
        _patch(cid, collected_issues="Knights 1, Knights 2, Knights 3",
               format="trade paperback")
        flagged = _flagged_for([cid])
        assert flagged
        assert "prose_collects" in {r.code for r in flagged[0].reasons}


def test_no_flag_for_clean_per_line_collected_issues():
    with _client() as client:
        cid = _save(client, title="IC Clean", isbn_13="9799300000003",
                    series="IC Clean Series")
        _patch(cid, collected_issues="Knights 1\nKnights 2\nKnights 3",
               format="trade paperback")
        # `format=trade paperback` and source_id=None means heuristics 2/3
        # don't fire either — the comic should be clean.
        flagged = _flagged_for([cid])
        assert not flagged


# ── Heuristic 2: format_collects_mismatch ──────────────────────────────


def test_flag_format_collects_mismatch_when_single_issue_has_collected():
    with _client() as client:
        cid = _save(client, title="IC Mismatch", isbn_13="9799300000101",
                    series="IC Mismatch Series")
        _patch(cid, collected_issues="Issue 1\nIssue 2", format="single issue")
        flagged = _flagged_for([cid])
        codes = {r.code for r in flagged[0].reasons}
        assert "format_collects_mismatch" in codes


# ── Heuristic 3: single_issue_pattern_with_trade_format ────────────────


def test_flag_single_issue_article_pattern_when_format_is_trade():
    """The High Republic case: format=TPB but source_id ends with a
    trailing small integer and has no Vol./Volume marker."""
    with _client() as client:
        cid = _save(client, title="IC Pattern", isbn_13="9799300000201",
                    series="IC Pattern Series")
        _patch(
            cid,
            format="trade paperback",
            source="wookieepedia",
            source_id="The High Republic (2023) 1",
        )
        flagged = _flagged_for([cid])
        codes = {r.code for r in flagged[0].reasons}
        assert "single_issue_pattern_with_trade_format" in codes


def test_no_flag_when_source_id_has_volume_marker():
    """Trade article titles like 'Knights of the Old Republic Vol. 1'
    legitimately end in a number but contain a Vol. marker — exempt."""
    with _client() as client:
        cid = _save(client, title="IC VolOK", isbn_13="9799300000202",
                    series="IC VolOK Series")
        _patch(
            cid,
            format="trade paperback",
            source="wookieepedia",
            source_id="Star Wars: The High Republic Vol. 1",
        )
        flagged = _flagged_for([cid])
        # Heuristic 3 must NOT fire — the source_id is fine.
        codes = {r.code for fc in flagged for r in fc.reasons}
        assert "single_issue_pattern_with_trade_format" not in codes


# ── Heuristic 4: cover_date_year_mismatch ──────────────────────────────


def test_flag_cover_date_year_mismatch_for_outlier_in_series():
    with _client() as client:
        # Seed 5 comics with 2015 cover dates + 1 outlier from 2025.
        ids: list[int] = []
        for i in range(5):
            cid = _save(client, title=f"IC YearN{i}",
                        isbn_13=f"97993000003{i:02d}",
                        series="IC Year Series")
            _patch(cid, cover_date=date(2015, 1, 1))
            ids.append(cid)
        outlier = _save(client, title="IC YearOutlier",
                        isbn_13="9799300000399", series="IC Year Series")
        _patch(outlier, cover_date=date(2025, 1, 1))
        ids.append(outlier)

        flagged = _flagged_for(ids)
        outlier_flags = [fc for fc in flagged if fc.comic.id == outlier]
        assert outlier_flags
        codes = {r.code for r in outlier_flags[0].reasons}
        assert "cover_date_year_mismatch" in codes
        # The 2015s should not be flagged on this heuristic.
        for fc in flagged:
            if fc.comic.id != outlier:
                assert "cover_date_year_mismatch" not in {r.code for r in fc.reasons}


# ── /admin/inconsistencies endpoint ────────────────────────────────────


def test_inconsistencies_endpoint_renders_flagged_rows_with_review_link(monkeypatch):
    with _client() as client:
        cid = _save(client, title="IC Render", isbn_13="9799300000401",
                    series="IC Render Series")
        _patch(cid, collected_issues="COLLECTING: Foo 1-3", format="single issue")

        r = client.get("/admin/inconsistencies")
        assert r.status_code == 200
        assert "IC Render" in r.text
        # Review button links to the comic's repick page.
        assert f'href="/comic/{cid}/repick"' in r.text


def test_inconsistencies_endpoint_renders_empty_state_when_clean():
    """A degenerate "everything is fine" check — we can't actually achieve
    this in the shared test DB, so this test just confirms the endpoint
    survives an empty result by mocking the service to return []."""
    import app.services.inconsistencies as svc

    async def fake_find(*args, **kw):
        return []
    with _client() as client:
        # Use monkeypatch via attribute swap (testclient lifespan keeps the
        # app alive, so direct attr replacement on the module is fine).
        original = svc.find_inconsistencies
        svc.find_inconsistencies = fake_find
        try:
            r = client.get("/admin/inconsistencies")
            assert r.status_code == 200
            assert "No inconsistencies found" in r.text
        finally:
            svc.find_inconsistencies = original


def test_admin_page_includes_inconsistencies_button():
    with _client() as client:
        r = client.get("/admin")
        assert r.status_code == 200
        assert "Find inconsistencies" in r.text
        assert 'hx-get="/admin/inconsistencies"' in r.text
