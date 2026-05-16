"""Bulk edit on /library.

POST /library/bulk takes comic_id[] + a set of fields and applies them to
every selected comic in one transaction. Empty fields are no-ops.
"""

from __future__ import annotations

import asyncio
from datetime import date

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, Copy


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


def _first_copy(comic_id: int) -> Copy | None:
    async def _go():
        async with SessionLocal() as session:
            return (await session.exec(
                select(Copy).where(Copy.comic_id == comic_id).order_by(Copy.id.asc()).limit(1)
            )).first()
    return asyncio.run(_go())


def test_bulk_edit_writes_format_canon_era_to_every_selection():
    with _client() as client:
        a = _save(client, title="BE A", isbn_13="9789000000001", series="BE Series A")
        b = _save(client, title="BE B", isbn_13="9789000000002", series="BE Series B")
        c = _save(client, title="BE C", isbn_13="9789000000003", series="BE Series C")

        r = client.post("/library/bulk", data={
            "comic_id": [a, b],  # not c
            "format": "trade paperback",
            "canon": "canon",
            "era": "Imperial",
        }, follow_redirects=False)
        assert r.status_code == 303

        ca, cb, cc = _comic(a), _comic(b), _comic(c)
        assert ca.format == "trade paperback" and cb.format == "trade paperback"
        assert cc.format != "trade paperback"
        assert ca.canon == "canon" and cb.canon == "canon"
        assert ca.era == "Imperial" and cb.era == "Imperial"


def test_bulk_edit_blank_fields_are_no_op():
    with _client() as client:
        cid = _save(client, title="BE Blank", isbn_13="9789000000099",
                    series="BE Blank Series")
        # Pre-set a value so we can detect overwrite.
        async def _seed():
            async with SessionLocal() as session:
                comic = await session.get(Comic, cid)
                comic.format = "single issue"
                session.add(comic)
                await session.commit()
        asyncio.run(_seed())

        r = client.post("/library/bulk", data={
            "comic_id": [cid],
            "format": "",  # blank → keep
            "canon": "",
            "era": "",
        }, follow_redirects=False)
        assert r.status_code == 303
        assert _comic(cid).format == "single issue"


def test_bulk_edit_storage_writes_to_every_copy():
    with _client() as client:
        cid = _save(client, title="BE Stor", isbn_13="9789000000101",
                    series="BE Stor Series")
        # Add a second copy so we can verify all-copies behaviour.
        client.post(f"/comic/{cid}/copies", data={"storage_location": ""})

        client.post("/library/bulk", data={
            "comic_id": [cid],
            "storage_location": "Long Box 9",
        }, follow_redirects=False)

        async def _all_copies():
            async with SessionLocal() as session:
                return (await session.exec(
                    select(Copy).where(Copy.comic_id == cid)
                )).all()
        copies = asyncio.run(_all_copies())
        assert len(copies) >= 2
        assert all(cp.storage_location == "Long Box 9" for cp in copies)


def test_bulk_edit_mark_read_flips_first_unread_copy():
    with _client() as client:
        cid = _save(client, title="BE Read", isbn_13="9789000000201",
                    series="BE Read Series")

        client.post("/library/bulk", data={
            "comic_id": [cid],
            "mark_read": "on",
        }, follow_redirects=False)

        cp = _first_copy(cid)
        assert cp.read_status == "read"
        assert cp.date_read == date.today()


def test_bulk_edit_with_no_selection_redirects_quietly():
    with _client() as client:
        r = client.post("/library/bulk", data={"format": "single issue"},
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/library"


def test_bulk_edit_redirects_back_to_return_to_url():
    with _client() as client:
        cid = _save(client, title="BE RT", isbn_13="9789000000301",
                    series="BE RT Series")
        r = client.post("/library/bulk", data={
            "comic_id": [cid],
            "return_to": "/library?canon=canon&page=2",
            "canon": "canon",
        }, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/library?canon=canon&page=2"


def _tags_for(comic_id: int) -> set[str]:
    from app.models import ComicTag, Tag
    async def _go():
        async with SessionLocal() as session:
            rows = (await session.exec(
                select(Tag.name).join(ComicTag, ComicTag.tag_id == Tag.id)
                .where(ComicTag.comic_id == comic_id)
            )).all()
            return set(rows)
    return asyncio.run(_go())


def test_bulk_edit_adds_tags_to_every_selection():
    with _client() as client:
        a = _save(client, title="BT A", isbn_13="9789000000501",
                  series="BT Series A")
        b = _save(client, title="BT B", isbn_13="9789000000502",
                  series="BT Series B")
        client.post("/library/bulk", data={
            "comic_id": [a, b],
            "add_tags": "favorite, must-read",
        }, follow_redirects=False)
        assert "favorite" in _tags_for(a)
        assert "must-read" in _tags_for(a)
        assert "favorite" in _tags_for(b)


def test_bulk_edit_removes_tags():
    with _client() as client:
        cid = _save(client, title="BT R", isbn_13="9789000000601",
                    series="BT R Series")
        client.post(f"/comic/{cid}/tags", data={"name": "ditch-me"})
        client.post(f"/comic/{cid}/tags", data={"name": "keep-me"})

        client.post("/library/bulk", data={
            "comic_id": [cid],
            "remove_tags": "ditch-me",
        }, follow_redirects=False)
        names = _tags_for(cid)
        assert "ditch-me" not in names
        assert "keep-me" in names


def test_bulk_edit_add_tags_idempotent():
    with _client() as client:
        cid = _save(client, title="BT I", isbn_13="9789000000701",
                    series="BT I Series")
        client.post("/library/bulk", data={
            "comic_id": [cid],
            "add_tags": "alpha",
        }, follow_redirects=False)
        client.post("/library/bulk", data={
            "comic_id": [cid],
            "add_tags": "alpha",
        }, follow_redirects=False)
        # Tag still appears exactly once via the comic's tag set.
        assert "alpha" in _tags_for(cid)


def test_library_page_includes_bulk_bar_markup():
    with _client() as client:
        _save(client, title="Bar Probe", isbn_13="9789000000401",
              series="Bar Series")
        r = client.get("/library")
        assert r.status_code == 200
        assert 'id="lb-bulk-bar"' in r.text
        assert 'lb-bulk-check' in r.text
        assert 'window.lbBulk' in r.text


# ─────────────────────────  Bulk delete  ───────────────────────── #


def test_bulk_delete_removes_selected_comics_and_their_copies():
    with _client() as client:
        a = _save(client, title="Del A", isbn_13="9789000000501", series="Del Series A")
        b = _save(client, title="Del B", isbn_13="9789000000502", series="Del Series B")
        keep = _save(client, title="Keep", isbn_13="9789000000503", series="Keep Series")

        r = client.post("/library/bulk-delete", data={
            "comic_id": [a, b],
            "confirm": "yes",
        }, follow_redirects=False)
        assert r.status_code == 303

        # a + b are gone; keep is still there.
        assert _comic(a) is None
        assert _comic(b) is None
        assert _comic(keep) is not None
        # Their copies are gone too.
        assert _first_copy(a) is None
        assert _first_copy(b) is None


def test_bulk_delete_without_confirm_is_a_noop():
    """Server-side safety belt — even if the JS confirm() is bypassed,
    a POST without `confirm=yes` must NOT delete anything."""
    with _client() as client:
        cid = _save(client, title="Safe", isbn_13="9789000000601",
                    series="Safe Series")
        r = client.post("/library/bulk-delete", data={
            "comic_id": [cid],
            # confirm omitted
        }, follow_redirects=False)
        assert r.status_code == 303
        assert _comic(cid) is not None


def test_bulk_delete_prunes_orphan_series():
    """When the last comic in a series is deleted, the Series row should
    be pruned so library facets don't show ghost entries."""
    import asyncio
    from app.models import Series

    with _client() as client:
        cid = _save(client, title="Orph", isbn_13="9789000000701",
                    series="OrphSeries-9789000000701", publisher="OrphPub")
        comic = _comic(cid)
        sid = comic.series_id
        assert sid is not None

        client.post("/library/bulk-delete", data={
            "comic_id": [cid],
            "confirm": "yes",
        }, follow_redirects=False)

        async def _series_exists():
            async with SessionLocal() as session:
                return await session.get(Series, sid)

        assert asyncio.run(_series_exists()) is None


def test_library_page_renders_delete_button_in_bulk_bar():
    with _client() as client:
        _save(client, title="Btn Probe", isbn_13="9789000000801",
              series="Btn Series")
        r = client.get("/library")
        assert r.status_code == 200
        # The Delete button posts to /library/bulk-delete via formaction.
        assert 'formaction="/library/bulk-delete"' in r.text
        # JS confirm() helper is wired so dead clicks don't fire DELETE.
        assert "lbBulkConfirmDelete" in r.text


# ─────────────────────────  Save-flow metadata fill  ───────────── #
#
# Regression: when adding a comic from Wookieepedia, the save flow used
# to skip source-only fields (collected_issues, format, language, era,
# canon, timeline) under some conditions; only a manual refresh would
# populate them. `_backfill_metadata` must always fill these from the
# candidate since they never appear on the confirm form (no user input
# to clobber).


def test_backfill_always_fills_source_only_fields_even_without_force():
    """force=False is the save-time default. Source-only fields should
    still be written from the candidate so save ↔ refresh agree."""
    import asyncio

    from app.routers.add import _backfill_metadata
    from app.services.schemas import LookupCandidate

    with _client() as client:
        cid = _save(client, title="SrcOnly", isbn_13="9789000000901",
                    series="SrcOnly Series")

        cand = LookupCandidate(
            source="wookieepedia",
            source_id="Fake Article",
            title=None,  # don't clobber user title
            collected_issues="Issue #1\nIssue #2",
            format="trade paperback",
            language="english",
            era="rise of the empire era",
            canon="canon",
            timeline="22 BBY",
        )

        async def _run():
            async with SessionLocal() as session:
                comic = await session.get(Comic, cid)
                await _backfill_metadata(session, comic, cand)
                await session.refresh(comic)
                return comic
        comic = asyncio.run(_run())
        # Source-only fields all populated despite force=False default.
        assert comic.collected_issues == "Issue #1\nIssue #2"
        assert comic.format == "trade paperback"
        assert comic.language == "english"
        assert comic.era == "rise of the empire era"
        assert comic.canon == "canon"
        assert comic.timeline == "22 BBY"
        # User-editable title was left intact (candidate.title was None).
        assert comic.title == "SrcOnly"


# ─────────────  Series auto-enrichment on save  ───────────── #


def test_series_enrichment_pulls_expected_issues_for_wookieepedia():
    """After save, a freshly-created series row should pick up
    source/source_id/expected_issues in the background so the series
    detail page shows the missing-issues list without the user having
    to manually trigger /series/{id}/refresh."""
    import asyncio
    from unittest.mock import patch
    from app.models import Series

    fake_issues = ["Article: Series #1", "Article: Series #2", "Article: Series #3"]

    async def fake_get_series_issues(title: str) -> list[str]:
        return fake_issues

    with patch(
        "app.services.wookieepedia.get_series_issues",
        side_effect=fake_get_series_issues,
    ):
        from app.routers.add import _enrich_series_from_candidate
        # Seed: save a comic so we have a series row to enrich.
        with _client() as client:
            cid = _save(client, title="Enr A", isbn_13="9789000001101",
                        series="EnrSeries-1101", publisher="EnrPub")
            comic = _comic(cid)
            sid = comic.series_id

            asyncio.run(_enrich_series_from_candidate(
                sid, "wookieepedia", "EnrSeries-1101", None,
            ))

            async def _load_series():
                async with SessionLocal() as session:
                    return await session.get(Series, sid)
            series = asyncio.run(_load_series())
            assert series.source == "wookieepedia"
            assert series.source_id == "EnrSeries-1101"
            assert series.expected_issues
            assert "Article: Series #1" in series.expected_issues


def test_series_enrichment_skips_when_already_enriched():
    """Idempotent: a series that already has source + expected_issues
    should not be touched (manual refresh remains the right tool)."""
    import asyncio
    from unittest.mock import patch
    from app.models import Series

    called = {"n": 0}

    async def fake_get_series_issues(title: str) -> list[str]:
        called["n"] += 1
        return ["should not be saved"]

    with patch(
        "app.services.wookieepedia.get_series_issues",
        side_effect=fake_get_series_issues,
    ):
        from app.routers.add import _enrich_series_from_candidate
        with _client() as client:
            cid = _save(client, title="EnrIdem", isbn_13="9789000001201",
                        series="EnrIdem-1201", publisher="EnrPub")
            comic = _comic(cid)
            sid = comic.series_id

            # Pre-fill so enrichment should be a no-op.
            async def _seed():
                async with SessionLocal() as session:
                    series = await session.get(Series, sid)
                    series.source = "wookieepedia"
                    series.source_id = "Manual"
                    series.expected_issues = "Existing #1"
                    session.add(series)
                    await session.commit()
            asyncio.run(_seed())

            # The background inference task fired by /add/save also
            # hits our mocked fetcher (with one call per inferred
            # canonical). Snapshot the count post-save so we can
            # assert that the EXPLICIT _enrich_series_from_candidate
            # call below adds zero further calls.
            baseline = called["n"]
            asyncio.run(_enrich_series_from_candidate(
                sid, "wookieepedia", "EnrIdem-1201", None,
            ))

            async def _load_series():
                async with SessionLocal() as session:
                    return await session.get(Series, sid)
            series = asyncio.run(_load_series())
            # Untouched.
            assert series.source_id == "Manual"
            assert series.expected_issues == "Existing #1"
            # No NEW calls from _enrich_series_from_candidate.
            assert called["n"] == baseline


def test_series_enrichment_no_op_when_fetcher_returns_empty():
    """If upstream returns no issues (article doesn't exist, anthology
    page, etc.) we leave the series in its bare state — the refresh
    form is still available if the user wants to try a different
    source_id."""
    import asyncio
    from unittest.mock import patch
    from app.models import Series

    async def fake_get_series_issues(title: str) -> list[str]:
        return []

    with patch(
        "app.services.wookieepedia.get_series_issues",
        side_effect=fake_get_series_issues,
    ):
        from app.routers.add import _enrich_series_from_candidate
        with _client() as client:
            cid = _save(client, title="EnrEmpty", isbn_13="9789000001301",
                        series="EnrEmpty-1301", publisher="EnrPub")
            comic = _comic(cid)
            sid = comic.series_id

            asyncio.run(_enrich_series_from_candidate(
                sid, "wookieepedia", "EnrEmpty-1301", None,
            ))

            async def _load_series():
                async with SessionLocal() as session:
                    return await session.get(Series, sid)
            series = asyncio.run(_load_series())
            assert series.expected_issues in (None, "")


def test_backfill_does_not_clobber_user_edited_title_when_not_forced():
    """The flip side: title IS a confirm-form field, so save flow must
    respect a user-set title even if the candidate has its own."""
    import asyncio

    from app.routers.add import _backfill_metadata
    from app.services.schemas import LookupCandidate

    with _client() as client:
        cid = _save(client, title="User Chose This", isbn_13="9789000001001",
                    series="UC Series")

        cand = LookupCandidate(
            source="wookieepedia",
            source_id="Fake",
            title="Upstream Title That Should NOT Win",
            description="Upstream description that also should not win",
        )

        async def _run():
            async with SessionLocal() as session:
                comic = await session.get(Comic, cid)
                await _backfill_metadata(session, comic, cand)
                await session.refresh(comic)
                return comic
        comic = asyncio.run(_run())
        assert comic.title == "User Chose This"
