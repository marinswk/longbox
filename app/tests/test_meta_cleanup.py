"""Series/comic name cleanup + collected-issues display polish.

Two fixes share a root cause: data from upstream (or the user's CSV)
sometimes lands in the DB with garbage that the display layer trusted
naively.

  1. Wookieepedia ComicBook articles whose `series=` infobox field
     carries a multi-value blob (e.g. an original title plus a
     re-launch) ended up creating Series rows whose `name` had a
     literal "\n" in it. Fix: parser takes the first non-empty line
     for single-value fields; lifespan backfill cleans existing rows.

  2. `Comic.collected_issues` from any source (CSV import, manual
     edit, Wookieepedia) can be Marvel-style "COLLECTING:" prose.
     The detail template used to wrap every line in a Wookieepedia
     article URL — broken link. Fix: a `parse_entries()` helper
     classifies each line as `linkable` or not; the template only
     wraps linkable entries in anchors.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, Publisher, Series
from app.services.collected_issues import parse_entries
from app.services.fandoms import backfill_strip_multiline_names


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "Test Publisher", "series": "Test Series"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


# ── parse_entries: classify by shape, not by source ─────────────────────


def test_parse_entries_marks_clean_titles_linkable():
    out = parse_entries("Knights of the Old Republic 1\nKnights of the Old Republic 2")
    assert all(e.linkable for e in out)
    assert [e.text for e in out] == [
        "Knights of the Old Republic 1",
        "Knights of the Old Republic 2",
    ]


def test_parse_entries_keeps_year_in_parens_linkable():
    out = parse_entries("Star Wars (1998) 7")
    assert len(out) == 1 and out[0].linkable
    assert out[0].text == "Star Wars (1998) 7"


def test_parse_entries_strips_collecting_prefix_and_marks_non_linkable():
    raw = ("COLLECTING: Star Wars: The High Republic (2023) #1-5, "
           "Star Wars: Revelations (2023) 1 (Story 6)")
    out = parse_entries(raw)
    assert len(out) == 1
    e = out[0]
    assert e.linkable is False
    # Original text is preserved verbatim so users see exactly what's stored.
    assert e.text == raw


def test_parse_entries_marks_comma_lists_non_linkable():
    out = parse_entries("Knights 1, Knights 2, Knights 3")
    assert len(out) == 1
    assert out[0].linkable is False


def test_parse_entries_marks_hash_ranges_non_linkable():
    out = parse_entries("Star Wars #1-14")
    assert out[0].linkable is False


def test_parse_entries_handles_empty_or_none():
    assert parse_entries(None) == []
    assert parse_entries("") == []
    assert parse_entries("\n\n  \n") == []


# ── Comic detail page renders parsed entries ────────────────────────────


def _seed_collected(comic_id: int, value: str):
    async def _go():
        async with SessionLocal() as session:
            c = await session.get(Comic, comic_id)
            c.collected_issues = value
            session.add(c)
            await session.commit()
    asyncio.run(_go())


def test_collected_prose_renders_as_plain_text_not_link():
    """Regression: a comic with 'COLLECTING:' prose used to render the
    whole string wrapped in a Wookieepedia URL — a link to a non-existent
    article. The smart parser should keep it as plain text."""
    with _client() as client:
        cid = _save(client, title="Prose Probe", isbn_13="9799000000401",
                    series="Prose Series")
        _seed_collected(cid, "COLLECTING: Star Wars (2015) #1-14, Star Wars Annual 1")

        # Force the comic to look like a Wookieepedia hit so the template's
        # `is_wookiee` flag fires.
        async def _wp_source():
            async with SessionLocal() as session:
                c = await session.get(Comic, cid)
                c.source = "wookieepedia"
                c.source_id = "Star_Wars_(2015)"
                session.add(c)
                await session.commit()
        asyncio.run(_wp_source())

        page = client.get(f"/comic/{cid}").text
        # The prose should NOT be wrapped in an <a href> Wookieepedia link.
        assert 'href="https://starwars.fandom.com/wiki/COLLECTING' not in page
        # But the text should still be visible.
        assert "Star Wars (2015) #1-14" in page


def test_collected_clean_titles_render_as_links_for_wookieepedia_source():
    with _client() as client:
        cid = _save(client, title="Link Probe", isbn_13="9799000000402",
                    series="Link Series")
        _seed_collected(cid, "Knights of the Old Republic 1\nKnights of the Old Republic 2")

        async def _wp_source():
            async with SessionLocal() as session:
                c = await session.get(Comic, cid)
                c.source = "wookieepedia"
                c.source_id = "Knights_of_the_Old_Republic_1"
                session.add(c)
                await session.commit()
        asyncio.run(_wp_source())

        page = client.get(f"/comic/{cid}").text
        # The wookiee_url macro replaces spaces with underscores before
        # URL-encoding — that's the canonical Wookieepedia article URL form.
        assert ('href="https://starwars.fandom.com/wiki/'
                'Knights_of_the_Old_Republic_1"') in page
        assert ('href="https://starwars.fandom.com/wiki/'
                'Knights_of_the_Old_Republic_2"') in page


# ── Lifespan backfill cleans multi-line names ──────────────────────────


def test_backfill_strips_newlines_from_series_name():
    """Mimics a Wookieepedia ComicBook article whose `series=` field had
    `Star Wars: The High Republic\\nStar Wars: The High Republic (2023)`
    saved as-is."""
    with _client():
        pass

    async def _seed():
        async with SessionLocal() as session:
            ser = Series(name="Star Wars: The High Republic \nStar Wars: The High Republic (2023)")
            session.add(ser)
            await session.commit()
            await session.refresh(ser)
            return ser.id
    sid = asyncio.run(_seed())

    n = asyncio.run(backfill_strip_multiline_names())
    assert n >= 1

    async def _read():
        async with SessionLocal() as session:
            return await session.get(Series, sid)
    cleaned = asyncio.run(_read())
    assert cleaned.name == "Star Wars: The High Republic"


def test_backfill_is_idempotent():
    asyncio.run(backfill_strip_multiline_names())
    n = asyncio.run(backfill_strip_multiline_names())
    assert n == 0
