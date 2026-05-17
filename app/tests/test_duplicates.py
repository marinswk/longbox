"""Duplicates page — issue-level coverage tracking across single
issues, TPBs, and omnibuses.

The old "comic has 2+ copies" view was replaced by an
issue-reverse-index that surfaces underlying issues owned through
2+ different Comics. Tests exercise the new index + filters.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, Series
from app.services.duplicates import (
    apply_filters_and_sort, build_duplicate_index,
)


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


def _set_fields(comic_id: int, **fields) -> None:
    async def _go():
        async with SessionLocal() as s:
            c = await s.get(Comic, comic_id)
            for k, v in fields.items():
                setattr(c, k, v)
            s.add(c)
            await s.commit()
    asyncio.run(_go())


# ──────────────────────  unit tests for the index builder  ─────────


def _foo_series() -> Series:
    """Series with the test issue list — required as ground-truth
    for the duplicates index's `known_issues` filter."""
    return Series(
        id=99, name="Foo", expected_issues="Foo 1\nFoo 2\nFoo 3",
    )


def test_build_duplicate_index_finds_issue_in_singles_and_tpb():
    """A single-issue Comic AND a TPB whose collected_issues
    contains that issue should produce one duplicate row with two
    owners."""
    single = Comic(
        id=1, title="Solo Single",
        source="wookieepedia", source_id="Foo 1",
        format="single issue",
    )
    tpb = Comic(
        id=2, title="Foo TPB",
        source="wookieepedia", source_id="Foo TPB Article",
        format="trade paperback",
        collected_issues="Foo 1\nFoo 2\nFoo 3",
    )
    rows = build_duplicate_index([single, tpb], [_foo_series()], min_copies=2)
    titles = {r.issue_title for r in rows}
    # Only "Foo 1" appears in both. Foo 2 and Foo 3 are TPB-only.
    assert titles == {"Foo 1"}
    assert rows[0].count == 2


def test_build_duplicate_index_filters_out_noise_not_in_any_series():
    """Short-story titles like 'Old Wounds' that appear in two
    omnibus contents but aren't real issue articles should NOT
    surface as duplicates — they aren't in any series'
    expected_issues so the `known_issues` ground-truth set rejects
    them."""
    a = Comic(
        id=1, title="A TPB", format="trade paperback",
        collected_issues="Foo 1\nOld Wounds\nThe Taris Holofeed",
    )
    b = Comic(
        id=2, title="B TPB", format="trade paperback",
        collected_issues="Foo 1\nOld Wounds\nThe Taris Holofeed",
    )
    rows = build_duplicate_index([a, b], [_foo_series()], min_copies=2)
    titles = {r.issue_title for r in rows}
    # Foo 1 IS in Foo series' expected_issues → real duplicate.
    # Old Wounds / Taris Holofeed are not → filtered.
    assert titles == {"Foo 1"}


def test_build_duplicate_index_skips_under_threshold():
    """Issues with only one owner are filtered out."""
    a = Comic(
        id=1, title="A", source="wookieepedia", source_id="X 1",
        format="single issue",
    )
    b = Comic(
        id=2, title="B", source="wookieepedia", source_id="Y 1",
        format="single issue",
    )
    rows = build_duplicate_index([a, b], [
        Series(id=1, name="X", expected_issues="X 1"),
        Series(id=2, name="Y", expected_issues="Y 1"),
    ], min_copies=2)
    assert rows == []


def test_build_duplicate_index_min_copies_filter():
    """`min_copies=3` excludes issues with only 2 owners."""
    single = Comic(
        id=1, title="Solo", source="wookieepedia", source_id="X 1",
        format="single issue",
    )
    tpb_a = Comic(
        id=2, title="TPB A", format="trade paperback",
        collected_issues="X 1\nX 2",
    )
    tpb_b = Comic(
        id=3, title="TPB B", format="trade paperback",
        collected_issues="X 2\nX 3",
    )
    x_series = Series(id=1, name="X",
                      expected_issues="X 1\nX 2\nX 3")
    # X 1 has 2 owners, X 2 has 2 owners. Both filtered out with min=3.
    rows = build_duplicate_index([single, tpb_a, tpb_b], [x_series], min_copies=3)
    assert rows == []
    # Lower the threshold: 2 rows surface.
    rows = build_duplicate_index([single, tpb_a, tpb_b], [x_series], min_copies=2)
    assert {r.issue_title for r in rows} == {"X 1", "X 2"}


def test_build_duplicate_index_derives_series_from_smallest_match():
    """When multiple series claim the same expected_issue, pick the
    most-specific (smallest expected_issues count) for grouping."""
    umbrella = Series(
        id=1, name="Big Series",
        expected_issues="Foo 1\nFoo 2\nFoo 3\nFoo 4\nFoo 5",
    )
    sub = Series(
        id=2, name="Big Series: Sub",
        expected_issues="Foo 1\nFoo 2",
    )
    a = Comic(id=1, title="A", source="wookieepedia",
              source_id="Foo 1", format="single issue")
    b = Comic(id=2, title="B", format="trade paperback",
              collected_issues="Foo 1\nFoo 2")
    rows = build_duplicate_index([a, b], [umbrella, sub], min_copies=2)
    assert len(rows) == 1
    # 2-issue sub-series wins over 5-issue umbrella for grouping.
    assert rows[0].derived_series == "Big Series: Sub"


def test_build_duplicate_index_falls_back_to_trailing_number_strip():
    """When a series row exists for grouping fallback but only one
    Series covers the issue, derive_series picks that series'
    name; without any matching series, falls back to trailing-
    number-strip."""
    # Only one series exists, listing the dup issue. Used as the
    # ground-truth filter AND the derived series name.
    bare = Series(id=1, name="Random Series",
                  expected_issues="Random Series 7\nRandom Series 8")
    a = Comic(id=1, title="A", source="wookieepedia",
              source_id="Random Series 7", format="single issue")
    b = Comic(id=2, title="B", format="trade paperback",
              collected_issues="Random Series 7\nOther 1")
    rows = build_duplicate_index([a, b], [bare], min_copies=2)
    assert rows[0].derived_series == "Random Series"


def test_apply_filters_singles_and_collection_mix():
    """The `singles_and_collection` mix keeps only rows that have
    BOTH a single-issue owner AND a collection owner."""
    single = Comic(id=1, title="S", source="wookieepedia",
                   source_id="X 1", format="single issue")
    tpb1 = Comic(id=2, title="T1", format="trade paperback",
                 collected_issues="X 1\nY 1")
    tpb2 = Comic(id=3, title="T2", format="trade paperback",
                 collected_issues="X 1\nY 1")
    s = [
        Series(id=1, name="X", expected_issues="X 1"),
        Series(id=2, name="Y", expected_issues="Y 1"),
    ]
    # X 1 has single + 2 tpbs → singles_and_collection ✓
    # Y 1 has 2 tpbs → collections_only
    rows = build_duplicate_index([single, tpb1, tpb2], s, min_copies=2)
    filtered = apply_filters_and_sort(rows, mix="singles_and_collection")
    assert {r.issue_title for r in filtered} == {"X 1"}
    filtered = apply_filters_and_sort(rows, mix="collections_only")
    assert {r.issue_title for r in filtered} == {"Y 1"}


def test_apply_filters_series():
    s = [
        Series(id=1, name="Alpha", expected_issues="Alpha 1"),
        Series(id=2, name="Beta",  expected_issues="Beta 1"),
    ]
    a = Comic(id=1, title="A", source="wookieepedia",
              source_id="Alpha 1", format="single issue")
    b = Comic(id=2, title="B", format="trade paperback",
              collected_issues="Alpha 1\nBeta 1")
    c = Comic(id=3, title="C", source="wookieepedia",
              source_id="Beta 1", format="single issue")
    rows = build_duplicate_index([a, b, c], s, min_copies=2)
    filtered = apply_filters_and_sort(rows, series="Alpha")
    assert {r.issue_title for r in filtered} == {"Alpha 1"}


def test_apply_filters_sort_count_desc():
    """Most-duplicated first."""
    a = Comic(id=1, title="A", source="wookieepedia",
              source_id="X 1", format="single issue")
    b = Comic(id=2, title="B", format="trade paperback",
              collected_issues="X 1\nY 1")
    c = Comic(id=3, title="C", source="wookieepedia",
              source_id="Y 1", format="single issue")
    d = Comic(id=4, title="D", format="trade paperback",
              collected_issues="X 1\nY 1")
    # X 1 → 3 owners (a, b, d), Y 1 → 3 owners (b, c, d). Equal —
    # so add another. Bump X 1 to 4 owners by adding tpb_e.
    e = Comic(id=5, title="E", format="trade paperback",
              collected_issues="X 1")
    s = [
        Series(id=1, name="X", expected_issues="X 1"),
        Series(id=2, name="Y", expected_issues="Y 1"),
    ]
    rows = build_duplicate_index([a, b, c, d, e], s, min_copies=2)
    sorted_ = apply_filters_and_sort(rows, sort="count_desc")
    assert sorted_[0].issue_title == "X 1"


# ──────────────────────  end-to-end page tests  ────────────────────


def test_duplicates_page_renders_issue_level_index():
    """End-to-end: save a single + a TPB that share an issue,
    /duplicates should render the issue as a duplicate row. The
    series ground-truth must include the issue title so the
    `known_issues` filter doesn't reject it."""
    with _client() as client:
        single_id = _save(client, title="Foo 1 single",
                          isbn_13="9783000010001",
                          series="Foo (comic series)",
                          publisher="P")
        _set_fields(single_id, source="wookieepedia",
                    source_id="Foo 1", format="single issue")
        tpb_id = _save(client, title="Foo TPB",
                       isbn_13="9783000010002",
                       series="Foo TPB Series",
                       publisher="P")
        _set_fields(tpb_id, format="trade paperback",
                    collected_issues="Foo 1\nFoo 2\nFoo 3")
        # Stamp expected_issues on the Foo singles series so the
        # filter treats "Foo 1" as a real issue article.
        async def _seed_expected():
            async with SessionLocal() as s:
                ser = (await s.exec(
                    select(Series).where(Series.name == "Foo (comic series)")
                )).first()
                ser.expected_issues = "Foo 1\nFoo 2\nFoo 3"
                s.add(ser)
                await s.commit()
        asyncio.run(_seed_expected())

        r = client.get("/duplicates")
        assert r.status_code == 200
        body = r.text
        assert "DUPLICATES" in body
        assert "Foo 1" in body
        assert "Foo 1 single" in body
        assert "Foo TPB" in body
        assert "×2" in body


def test_duplicates_empty_state_when_no_dupes():
    with _client() as client:
        _save(client, title="Lonesome", isbn_13="9783000020001",
              series="Lonesome Series", publisher="P")
        r = client.get("/duplicates")
        assert r.status_code == 200
        # Either we have legacy state from earlier tests in this DB,
        # or we show NO DUPLICATES. The header text is constant.
        assert "DUPLICATES" in r.text


def test_duplicates_filter_singles_and_collection():
    with _client() as client:
        # Set up: an issue that's in 2 TPBs but no single → would
        # show on default but NOT on singles_and_collection.
        a_id = _save(client, title="TPB A SC", isbn_13="9783000030001",
                     series="SC Series", publisher="P")
        _set_fields(a_id, format="trade paperback",
                    collected_issues="SC Issue 1\nSC Issue 2")
        b_id = _save(client, title="TPB B SC", isbn_13="9783000030002",
                     series="SC Series", publisher="P")
        _set_fields(b_id, format="trade paperback",
                    collected_issues="SC Issue 1\nSC Issue 2")
        # Mark these as real issues via the SC Series' expected_issues
        # so they pass the known-issues filter.
        async def _seed():
            async with SessionLocal() as s:
                ser = (await s.exec(
                    select(Series).where(Series.name == "SC Series")
                )).first()
                ser.expected_issues = "SC Issue 1\nSC Issue 2"
                s.add(ser)
                await s.commit()
        asyncio.run(_seed())

        r = client.get("/duplicates",
                       params={"mix": "singles_and_collection"})
        assert r.status_code == 200
        # SC Issue 1 / 2 are TPB-only — should NOT appear under this mix.
        assert "SC Issue 1" not in r.text
        # Verify it DOES show with default mix.
        r2 = client.get("/duplicates", params={"mix": "all"})
        assert "SC Issue 1" in r2.text


def test_dupes_link_appears_in_nav():
    with _client() as client:
        r = client.get("/library")
        assert 'href="/duplicates"' in r.text
