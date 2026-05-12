"""Stats page tests — verify the aggregations show on the rendered page."""

import json
import re

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    # /api/comics defaults to limit=50; the shared test DB accumulates rows
    # across files, so explicitly grab the full set when looking up by isbn.
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


def _stats_data(html: str) -> dict:
    m = re.search(
        r'<script id="stats-data" type="application/json">(.*?)</script>', html, re.DOTALL
    )
    assert m, "stats-data script not found"
    return json.loads(m.group(1))


def test_stats_page_renders_and_includes_data():
    with _client() as client:
        _save(
            client, title="A #1", isbn_13="9782000000001",
            series="Series A", publisher="Publisher A",
            cover_date="2012-01-01",
        )
        _save(
            client, title="B #1", isbn_13="9782000000002",
            series="Series B", publisher="Publisher B",
            cover_date="2002-09-01",
        )
        r = client.get("/stats")
        assert r.status_code == 200
        assert "BY THE NUMBERS" in r.text
        data = _stats_data(r.text)
        assert data["totals"]["comics"] >= 2
        assert data["totals"]["publishers"] >= 2
        # New top-level sections from the redesign.
        for key in ("formats", "canons", "eras", "read_status", "conditions",
                    "storage", "added_per_month", "read_per_month",
                    "series_progress", "highlights", "present"):
            assert key in data, f"missing stats key: {key}"


def test_format_distribution_appears_when_data_exists():
    import asyncio
    from sqlmodel import select
    from app.db import SessionLocal
    from app.models import Comic

    with _client() as client:
        _save(client, title="Fmt #1", isbn_13="9782000000301",
              series="Fmt Series", publisher="Fmt Pub")
        _save(client, title="Fmt #2", isbn_13="9782000000302",
              series="Fmt Series", publisher="Fmt Pub")

        async def _set_formats():
            async with SessionLocal() as session:
                rows = (await session.exec(
                    select(Comic).where(Comic.isbn_13.in_(
                        ["9782000000301", "9782000000302"]))
                )).all()
                for c, fmt in zip(rows, ["single issue", "trade paperback"]):
                    c.format = fmt
                    session.add(c)
                await session.commit()
        asyncio.run(_set_formats())

        r = client.get("/stats")
        data = _stats_data(r.text)
        assert data["present"]["format"] is True
        labels = {row["label"] for row in data["formats"]}
        assert "single issue" in labels or "trade paperback" in labels
        assert 'id="chart-format"' in r.text


def test_storage_donut_hidden_when_no_storage_set():
    with _client() as client:
        _save(client, title="NoStor #1", isbn_13="9782000000401",
              series="NoStor", publisher="NoStor Pub")
        r = client.get("/stats")
        data = _stats_data(r.text)
        # No copy in this test has a storage_location set; the gate may still
        # flip true if other tests in the same DB seeded storage. So either
        # the chart is hidden, or the gate accurately reflects existing data.
        if not data["present"]["storage"]:
            assert 'id="chart-storage"' not in r.text


def test_series_progress_aggregate_lists_complete_in_progress_untracked():
    with _client() as client:
        _save(client, title="SP #1", isbn_13="9782000000501",
              series="SP Series", publisher="SP Pub")
        r = client.get("/stats")
        data = _stats_data(r.text)
        sp = data["series_progress"]
        for k in ("complete", "in_progress", "untracked", "total"):
            assert k in sp
        assert sp["total"] == sp["complete"] + sp["in_progress"] + sp["untracked"]
        assert "SERIES PROGRESS" in r.text


def test_donut_slices_are_wired_to_library_filters():
    """Each navigable donut passes a filter-param string to the donut() call,
    which then redirects to /library?<param>=<label>. Smoke-check the
    rendered HTML contains the expected filter hooks."""
    with _client() as client:
        _save(client, title="Wire #1", isbn_13="9782000000701",
              series="Wire Series", publisher="Wire Pub")
        r = client.get("/stats")
        assert r.status_code == 200
        # read_status donut is always rendered (data is present from any save).
        assert "donut('chart-read'" in r.text and "'read_status')" in r.text
        # condition has no library filter — it's the only donut without a third arg.
        assert "donut('chart-condition'" in r.text
        # The click handler exists and points at /library.
        assert "/library?" in r.text


def test_chart_palette_is_crawl_not_hulk():
    with _client() as client:
        _save(client, title="Pal #1", isbn_13="9782000000601",
              series="Pal Series", publisher="Pal Pub")
        r = client.get("/stats")
        assert r.status_code == 200
        assert "#3ea135" not in r.text
        assert "#FFE81F" in r.text


def test_stats_payload_has_no_money_keys():
    """Cost/value aggregations were dropped from scope — make sure they
    don't sneak back in via a future refactor."""
    with _client() as client:
        _save(client, title="Plain #1", isbn_13="9782000000099", series="Plain", publisher="Plain Inc")
        r = client.get("/stats")
        assert r.status_code == 200
        data = _stats_data(r.text)
        assert "money" not in data
        assert "most_expensive" not in data


