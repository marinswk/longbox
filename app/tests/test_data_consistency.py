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
from app.services.fandoms import (
    backfill_merge_duplicate_series,
    backfill_single_issue_format,
    backfill_splice_year_in_comic_titles,
    backfill_strip_bogus_movie_adaptation_links,
    backfill_strip_umbrella_links_from_trades,
)
from app.models import ComicSeries
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


def test_strip_bogus_movie_adaptation_links_removes_unrelated_comics():
    """Earlier the wookieepedia parser dragged any comic that *collected*
    a film-adaptation tie-in (e.g. an Epic Collection holding
    'Episode I: The Phantom Menace ½') into the 'Star Wars Movie
    Adaptations' umbrella via ComicSeries. The fallback is now
    title-gated; this backfill sweeps the existing bogus links."""
    async def _seed():
        async with SessionLocal() as session:
            umbrella = Series(name="Star Wars Movie Adaptations")
            epic = Series(name="MA Epic Collection")
            session.add_all([umbrella, epic])
            await session.flush()

            # Real adaptation — title says so. Link must SURVIVE.
            real = Comic(
                series_id=umbrella.id, title="MA Rogue One Adaptation",
                isbn_13="9799500000001",
            )
            # GN trilogy — title says so. Link must SURVIVE.
            gn = Comic(
                series_id=umbrella.id, title="MA The Prequel Trilogy – A Graphic Novel",
                isbn_13="9799500000002",
            )
            # Bogus — Epic Collection, no 'Adaptation' / 'Graphic Novel'.
            # Primary series is the Epic Collection, but it also has a
            # stray ComicSeries link to the umbrella. Link must GO.
            bogus = Comic(
                series_id=epic.id, title="MA Epic Collection Rise of the Sith Vol. 2",
                isbn_13="9799500000003",
            )
            session.add_all([real, gn, bogus])
            await session.flush()

            session.add_all([
                ComicSeries(comic_id=real.id,  series_id=umbrella.id, is_primary=True),
                ComicSeries(comic_id=gn.id,    series_id=umbrella.id, is_primary=True),
                ComicSeries(comic_id=bogus.id, series_id=epic.id,     is_primary=True),
                ComicSeries(comic_id=bogus.id, series_id=umbrella.id, is_primary=False),
            ])
            await session.commit()
            return umbrella.id, bogus.id, real.id, gn.id

    umbrella_id, bogus_id, real_id, gn_id = asyncio.run(_seed())

    removed = asyncio.run(backfill_strip_bogus_movie_adaptation_links())
    assert removed == 1  # only the bogus link was dropped

    async def _check():
        async with SessionLocal() as session:
            rows = (await session.exec(
                select(ComicSeries.comic_id)
                .where(ComicSeries.series_id == umbrella_id)
            )).all()
            return {r if isinstance(r, int) else r[0] for r in rows}
    linked = asyncio.run(_check())
    assert real_id in linked
    assert gn_id in linked
    assert bogus_id not in linked

    # Idempotent.
    again = asyncio.run(backfill_strip_bogus_movie_adaptation_links())
    assert again == 0


def test_backfill_single_issue_format_fills_missing_singles_only():
    """Wookieepedia singles imported before the per-template default
    have format=NULL. The backfill flips them to 'single issue' while
    leaving trades (ISBN or collected_issues populated) and
    non-wookieepedia rows untouched."""
    with _client():
        pass

    async def _seed():
        async with SessionLocal() as session:
            # 3 wookieepedia singles with no format, ISBN, or contents.
            single1 = Comic(title="SI Singles A", source="wookieepedia",
                            source_id="SI_A", isbn_13="9799600000001")
            # Make this one a TRUE single (no ISBN) — overwrite below.
            single2 = Comic(title="SI Singles B", source="wookieepedia",
                            source_id="SI_B")
            single3 = Comic(title="SI Singles C", source="wookieepedia",
                            source_id="SI_C")
            # A trade (ISBN-13 present): must NOT be touched.
            trade = Comic(title="SI Trade", source="wookieepedia",
                          source_id="SI_Trade", isbn_13="9799600000099")
            # A trade with no ISBN but with collected_issues: must NOT be touched.
            trade2 = Comic(title="SI Trade Collected", source="wookieepedia",
                           source_id="SI_TC", collected_issues="Foo 1\nFoo 2")
            # A non-wookieepedia row with no format: must NOT be touched.
            non_wk = Comic(title="SI Non-WK", source="openlibrary",
                           source_id="X")
            # An already-formatted single: must NOT be touched.
            done = Comic(title="SI Already", source="wookieepedia",
                         source_id="SI_Done", format="trade paperback")
            session.add_all([single1, single2, single3, trade, trade2, non_wk, done])
            await session.flush()
            # Now scrub single1's ISBN so it counts as a true single.
            single1.isbn_13 = None
            session.add(single1)
            await session.commit()
            return {
                "singles": [single1.id, single2.id, single3.id],
                "trade": trade.id,
                "trade2": trade2.id,
                "non_wk": non_wk.id,
                "done": done.id,
            }
    ids = asyncio.run(_seed())

    n = asyncio.run(backfill_single_issue_format())
    assert n == 3

    async def _check():
        async with SessionLocal() as session:
            out = {}
            for sid in ids["singles"]:
                out[sid] = (await session.get(Comic, sid)).format
            out["trade"] = (await session.get(Comic, ids["trade"])).format
            out["trade2"] = (await session.get(Comic, ids["trade2"])).format
            out["non_wk"] = (await session.get(Comic, ids["non_wk"])).format
            out["done"] = (await session.get(Comic, ids["done"])).format
            return out
    fmts = asyncio.run(_check())
    for sid in ids["singles"]:
        assert fmts[sid] == "single issue", f"single {sid} not flipped"
    assert fmts["trade"] is None
    assert fmts["trade2"] is None
    assert fmts["non_wk"] is None
    assert fmts["done"] == "trade paperback"

    # Idempotent.
    again = asyncio.run(backfill_single_issue_format())
    assert again == 0


def test_strip_umbrella_links_removes_trades_only():
    """Category-backed umbrella series (One-shots, FCBD, Graphic
    Novels) should only contain actual one-shots / specials. The
    inferrer used to drag every trade collecting one such issue into
    the umbrella too; this sweep drops those bogus links while
    leaving the standalone one-shots in place."""
    async def _seed():
        async with SessionLocal() as session:
            umbrella = Series(
                name="UL Star Wars — One-shots",
                source="wookieepedia",
                source_id="Category:Canon one-shot comics",
            )
            tpb_series = Series(name="UL Some TPB Series")
            session.add_all([umbrella, tpb_series])
            await session.flush()

            # A real one-shot — primary series IS the umbrella.
            oneshot = Comic(
                title="UL Revelations 1",
                series_id=umbrella.id,
                isbn_13="9799700000001",
            )
            # A trade that collects that one-shot — primary series is
            # its own row; the bogus link is to the umbrella.
            trade = Comic(
                title="UL Big Omnibus Vol. 1",
                series_id=tpb_series.id,
                isbn_13="9799700000002",
                collected_issues="UL Revelations 1",
            )
            session.add_all([oneshot, trade])
            await session.flush()

            session.add_all([
                # One-shot's primary link to the umbrella — must SURVIVE.
                ComicSeries(comic_id=oneshot.id, series_id=umbrella.id, is_primary=True),
                # Trade's primary link to its own series — must SURVIVE.
                ComicSeries(comic_id=trade.id, series_id=tpb_series.id, is_primary=True),
                # Trade's stray non-primary link to the umbrella — must GO.
                ComicSeries(comic_id=trade.id, series_id=umbrella.id, is_primary=False),
            ])
            await session.commit()
            return umbrella.id, oneshot.id, trade.id

    umbrella_id, oneshot_id, trade_id = asyncio.run(_seed())

    n = asyncio.run(backfill_strip_umbrella_links_from_trades())
    assert n == 1

    async def _check():
        async with SessionLocal() as session:
            rows = (await session.exec(
                select(ComicSeries.comic_id, ComicSeries.is_primary)
                .where(ComicSeries.series_id == umbrella_id)
            )).all()
            return rows
    rows = asyncio.run(_check())
    comic_ids = {r[0] for r in rows}
    assert oneshot_id in comic_ids  # standalone one-shot still linked
    assert trade_id not in comic_ids  # trade's stray link gone

    # Idempotent.
    again = asyncio.run(backfill_strip_umbrella_links_from_trades())
    assert again == 0


def test_backfill_splice_year_rewrites_legacy_titles():
    """Comics added before the year-splice landed on the parser have
    titles missing the article's (YYYY) disambiguator. The backfill
    rewrites them in-place so 2022 and 2023 variants of the same
    one-shot are distinguishable."""
    async def _seed():
        async with SessionLocal() as session:
            # Needs fixing — source_id has year, title doesn't.
            c1 = Comic(title="YR Revelations 1", source="wookieepedia",
                       source_id="YR Revelations (2022) 1", isbn_13="9799800000001")
            # Already correct — must NOT be touched.
            c2 = Comic(title="YR Revelations (2022) 1", source="wookieepedia",
                       source_id="YR Revelations (2022) 1", isbn_13="9799800000002")
            # Source has no year — must NOT be touched.
            c3 = Comic(title="YR WBH 5", source="wookieepedia",
                       source_id="YR WBH 5", isbn_13="9799800000003")
            # Non-wookieepedia — must NOT be touched.
            c4 = Comic(title="YR Some 1", source="openlibrary",
                       source_id="X (2020) 1", isbn_13="9799800000004")
            session.add_all([c1, c2, c3, c4])
            await session.commit()
            return c1.id, c2.id, c3.id, c4.id

    cid1, cid2, cid3, cid4 = asyncio.run(_seed())

    n = asyncio.run(backfill_splice_year_in_comic_titles())
    assert n == 1

    async def _titles(ids):
        async with SessionLocal() as session:
            out = {}
            for i in ids:
                c = await session.get(Comic, i)
                out[i] = c.title if c else None
            return out

    titles = asyncio.run(_titles([cid1, cid2, cid3, cid4]))
    assert titles[cid1] == "YR Revelations (2022) 1"
    assert titles[cid2] == "YR Revelations (2022) 1"
    assert titles[cid3] == "YR WBH 5"
    assert titles[cid4] == "YR Some 1"

    # Idempotent.
    again = asyncio.run(backfill_splice_year_in_comic_titles())
    assert again == 0


def test_edit_form_includes_publisher_and_series_fields():
    with _client() as client:
        cid = _save(client, title="ED Form", isbn_13="9799400000401",
                    series="ED Form Series", publisher="ED Form Pub")
        page = client.get(f"/comic/{cid}/edit").text
        assert 'name="publisher"' in page
        assert 'name="series_name"' in page
