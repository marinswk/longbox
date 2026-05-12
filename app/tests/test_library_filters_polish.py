"""Polish on the library filter sidebar:

* Year facet is sorted by year descending (not by count).
* `format` is normalized to lowercase canonical form on every write site
  so chips don't fragment across casing variants.
* The lifespan backfill rewrites legacy mixed-case values.
"""

from __future__ import annotations

import asyncio
import re

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic
from app.services.csv_import import translate_format
from app.services.fandoms import backfill_normalize_format


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "Test Publisher", "series": "Test Series"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


# ── translate_format normalizes everything ──────────────────────────────


def test_translate_format_handles_arbitrary_casing_and_whitespace():
    assert translate_format("Trade Paperback") == "trade paperback"
    assert translate_format("TRADE PAPERBACK") == "trade paperback"
    assert translate_format("  trade   paperback  ") == "trade paperback"
    assert translate_format("Hardcover") == "hardcover"
    assert translate_format("Single Issue") == "single issue"
    assert translate_format("") is None
    assert translate_format(None) is None
    # Unknown values stay (lowercased + whitespace-collapsed) so we don't
    # silently lose user data.
    assert translate_format("Floppy Variant") == "floppy variant"


# ── Year facet sort order ───────────────────────────────────────────────


def test_year_facet_is_sorted_descending_by_year():
    """The library page should list newer years first regardless of their
    relative comic count."""
    with _client() as client:
        # Two comics in 2010, three in 2024 — count would put 2024 first
        # anyway, so seed three years to make sort vs. count distinguishable.
        _save(client, title="Old", isbn_13="9799000000001",
              series="OldS", publisher="Pub",
              cover_date="1995-01-01")
        _save(client, title="Mid1", isbn_13="9799000000002",
              series="MidS", publisher="Pub",
              cover_date="2010-01-01")
        _save(client, title="Mid2", isbn_13="9799000000003",
              series="MidS", publisher="Pub",
              cover_date="2010-06-01")
        _save(client, title="New", isbn_13="9799000000004",
              series="NewS", publisher="Pub",
              cover_date="2024-09-01")

        page = client.get("/library").text
        # Find the Year facet checkbox sequence and assert the year values
        # come out newest → oldest.
        years_in_order = re.findall(r'name="year" value="(\d+)"', page)
        # Filter to just our seeded set so unrelated tests' rows don't matter.
        ours = [y for y in years_in_order if y in ("1995", "2010", "2024")]
        # Each year appears exactly once; we sort within the facet only.
        first_idx = {y: ours.index(y) for y in ("1995", "2010", "2024") if y in ours}
        assert first_idx["2024"] < first_idx["2010"] < first_idx["1995"]


# ── Edit + bulk-edit normalize format ───────────────────────────────────


def _comic(comic_id: int) -> Comic:
    async def _go():
        async with SessionLocal() as session:
            return await session.get(Comic, comic_id)
    return asyncio.run(_go())


def test_edit_endpoint_lowercases_format_field():
    with _client() as client:
        cid = _save(client, title="Edit Norm", isbn_13="9799000000101",
                    series="Edit Norm Series")
        client.post(f"/comic/{cid}/edit", data={
            "title": "Edit Norm",
            "format": "Trade Paperback",
        })
        assert _comic(cid).format == "trade paperback"


def test_bulk_edit_lowercases_format_field():
    with _client() as client:
        cid = _save(client, title="Bulk Norm", isbn_13="9799000000201",
                    series="Bulk Norm Series")
        r = client.post("/library/bulk", data={
            "comic_id": [cid],
            "format": "HARDCOVER",
        }, follow_redirects=False)
        assert r.status_code == 303
        assert _comic(cid).format == "hardcover"


# ── Lifespan backfill rewrites legacy mixed-case values ────────────────


def test_backfill_normalize_format_rewrites_existing_rows():
    with _client():
        pass  # ensure tables exist

    async def _seed():
        async with SessionLocal() as session:
            comic = Comic(title="Legacy Cap", format="Trade Paperback",
                          fandom=None)
            session.add(comic)
            await session.commit()
            await session.refresh(comic)
            return comic.id
    cid = asyncio.run(_seed())

    n = asyncio.run(backfill_normalize_format())
    assert n >= 1
    assert _comic(cid).format == "trade paperback"

    # Idempotent: a second pass changes nothing.
    second = asyncio.run(backfill_normalize_format())
    assert second == 0


# ── Filter sidebar markup uses the new collapsible <details> structure ──


def test_library_sidebar_uses_collapsible_filter_sections():
    with _client() as client:
        _save(client, title="Sidebar A", isbn_13="9799000000301",
              series="Sidebar Series", publisher="Sidebar Pub",
              fandom="star wars sidebar")
        page = client.get("/library").text
        # Each facet is wrapped in a <details class="lb-filter ..."> block.
        assert page.count('class="lb-filter') >= 3
        # Tag list (if rendered) includes a client-side search input.
        # Sidebar Series doesn't have tags, but other tests pollute the DB
        # so we should usually see a Tag facet here. Either way the facet
        # list class should be present.
        assert "lb-facet-list" in page
