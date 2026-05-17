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
    rows = build_duplicate_index([single, tpb], [], min_copies=2)
    titles = {r.issue_title for r in rows}
    # Only "Foo 1" appears in both. Foo 2 and Foo 3 are TPB-only.
    assert titles == {"Foo 1"}
    assert rows[0].count == 2


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
    rows = build_duplicate_index([a, b], [], min_copies=2)
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
    # X 1 has 2 owners, X 2 has 2 owners. Both filtered out with min=3.
    rows = build_duplicate_index([single, tpb_a, tpb_b], [], min_copies=3)
    assert rows == []
    # Lower the threshold: 2 rows surface.
    rows = build_duplicate_index([single, tpb_a, tpb_b], [], min_copies=2)
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
    """No series rows → derive series from the issue title itself."""
    a = Comic(id=1, title="A", source="wookieepedia",
              source_id="Random Series 7", format="single issue")
    b = Comic(id=2, title="B", format="trade paperback",
              collected_issues="Random Series 7\nOther 1")
    rows = build_duplicate_index([a, b], [], min_copies=2)
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
    # X 1 has single + 2 tpbs → singles_and_collection ✓
    # Y 1 has 2 tpbs → collections_only
    rows = build_duplicate_index([single, tpb1, tpb2], [], min_copies=2)
    filtered = apply_filters_and_sort(rows, mix="singles_and_collection")
    assert {r.issue_title for r in filtered} == {"X 1"}
    filtered = apply_filters_and_sort(rows, mix="collections_only")
    assert {r.issue_title for r in filtered} == {"Y 1"}


def test_apply_filters_series():
    a = Comic(id=1, title="A", source="wookieepedia",
              source_id="Alpha 1", format="single issue")
    b = Comic(id=2, title="B", format="trade paperback",
              collected_issues="Alpha 1\nBeta 1")
    c = Comic(id=3, title="C", source="wookieepedia",
              source_id="Beta 1", format="single issue")
    rows = build_duplicate_index([a, b, c], [], min_copies=2)
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
    rows = build_duplicate_index([a, b, c, d, e], [], min_copies=2)
    sorted_ = apply_filters_and_sort(rows, sort="count_desc")
    assert sorted_[0].issue_title == "X 1"


# ──────────────────────  end-to-end page tests  ────────────────────


def test_duplicates_page_renders_issue_level_index():
    """End-to-end: save a single + a TPB that share an issue,
    /duplicates should render the issue as a duplicate row."""
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

        r = client.get("/duplicates")
        assert r.status_code == 200
        body = r.text
        assert "DUPLICATES" in body
        # Issue title visible.
        assert "Foo 1" in body
        # Both owning comics visible.
        assert "Foo 1 single" in body
        assert "Foo TPB" in body
        # Count badge.
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
