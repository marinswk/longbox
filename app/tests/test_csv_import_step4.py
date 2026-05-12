"""Step 4 of the CSV import wizard: per-row resolve.

Covers:
  * `find_candidates_multi` orchestration + ranker (with monkeypatched
    aggregator helpers so tests don't hit the network).
  * GET /resolve renders one card per row with `hx-trigger="revealed"` for
    lazy search.
  * POST /rows/{id}/search runs the search and persists candidates.
  * POST /rows/{id}/pick / /skip / /as-is move the row through the state
    machine.
  * The sticky progress footer reflects up-to-date counts after any change.
"""

from __future__ import annotations

import asyncio
import io
import json
from typing import Iterable

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import ImportRow, ImportSession
from app.services.aggregator import LookupResult, find_candidates_multi
from app.services.schemas import LookupCandidate


def _client() -> TestClient:
    return TestClient(create_app())


def _post_csv(client: TestClient, content: str) -> str:
    r = client.post(
        "/admin/import/csv",
        files={"file": ("t.csv", io.BytesIO(content.encode("utf-8")), "text/csv")},
        follow_redirects=False,
    )
    return r.headers["location"].split("/")[-2]


def _drive_to_resolve(client: TestClient, csv_text: str, sources: Iterable[str] = ("comicvine",)) -> str:
    """Walk a CSV through upload + map + config so we land on /resolve."""
    token = _post_csv(client, csv_text)
    # Map every CSV header to its obvious target.
    headers = csv_text.splitlines()[0].split(",")
    mapping: dict[str, str] = {}
    for h in headers:
        h = h.strip()
        if h.lower() in ("series", "seriesname"):
            mapping["series"] = h
        elif h.lower() == "title":
            mapping["title"] = h
        elif h.lower() in ("issue number", "issuenumber", "issue"):
            mapping["issue_number"] = h
        elif h.lower() in ("series year", "year"):
            mapping["year"] = h
        elif h.lower() == "publisher":
            mapping["publisher"] = h
        elif h.lower() == "fandom":
            mapping["fandom"] = h
    client.post(
        f"/admin/import/csv/{token}/map",
        data={f"map[{k}]": v for k, v in mapping.items()},
        follow_redirects=False,
    )
    src_data = {f"source[{s}]": "on" for s in sources}
    client.post(
        f"/admin/import/csv/{token}/config",
        data={**src_data, "year_tolerance": "1", "auto_tag_fandom": "on"},
        follow_redirects=False,
    )
    return token


# ── find_candidates_multi (pure orchestration) ──────────────────────────


def _cand(source: str, source_id: str, **kw) -> LookupCandidate:
    return LookupCandidate(source=source, source_id=source_id, **kw)


@pytest.mark.asyncio
async def test_multi_search_prefers_isbn_when_present(monkeypatch):
    calls: list[str] = []

    async def fake_lookup_full(ident: str, *, sources=None):
        calls.append(f"lookup_full:{ident}")
        return LookupResult(candidates=[_cand("openlibrary", "OL1", title="Foo")])

    async def fake_search_text(q: str, *, sources=None):  # pragma: no cover — must NOT be called
        calls.append(f"search_text:{q}")
        return LookupResult()

    monkeypatch.setattr("app.services.aggregator.lookup_full", fake_lookup_full)
    monkeypatch.setattr("app.services.aggregator.search_text", fake_search_text)

    r = await find_candidates_multi(
        series="Foo", title="Foo Vol. 1", isbn="9780000000001",
    )
    assert any("lookup_full:9780000000001" in c for c in calls)
    assert all("search_text:" not in c for c in calls)
    assert len(r.candidates) == 1


@pytest.mark.asyncio
async def test_multi_search_falls_back_to_text_when_no_isbn(monkeypatch):
    captured: list[str] = []

    async def fake_search_text(q: str, *, sources=None):
        captured.append(q)
        return LookupResult(candidates=[
            _cand("comicvine", "1", title="Skywalker Strikes",
                  series="Star Wars", cover_date="2015-01-01"),
        ])

    async def fake_lookup_full(ident: str, *, sources=None):  # pragma: no cover
        return LookupResult()

    monkeypatch.setattr("app.services.aggregator.search_text", fake_search_text)
    monkeypatch.setattr("app.services.aggregator.lookup_full", fake_lookup_full)

    r = await find_candidates_multi(
        series="Star Wars", title="Skywalker Strikes",
        sources=["comicvine"],
    )
    # First query is "series + title", which is enough to short-circuit.
    assert captured[0] == "Star Wars Skywalker Strikes"
    assert len(r.candidates) == 1


@pytest.mark.asyncio
async def test_multi_search_filters_unselected_sources(monkeypatch):
    async def fake_search_text(q: str, *, sources=None):
        return LookupResult(candidates=[
            _cand("wookieepedia", "Skywalker_Strikes", series="Star Wars"),
            _cand("comicvine", "1", series="Star Wars"),
            _cand("metron", "9", series="Star Wars"),
        ])
    async def fake_lookup_full(ident: str, *, sources=None):
        return LookupResult()
    monkeypatch.setattr("app.services.aggregator.search_text", fake_search_text)
    monkeypatch.setattr("app.services.aggregator.lookup_full", fake_lookup_full)

    r = await find_candidates_multi(
        series="Star Wars", title="X", sources=["comicvine"],
    )
    assert [c.source for c in r.candidates] == ["comicvine"]


@pytest.mark.asyncio
async def test_multi_search_passes_chosen_sources_through(monkeypatch):
    """The aggregator must give the underlying source-fanout fns the
    `sources=` set so they skip API calls for unselected sources."""
    seen: list[set | None] = []

    async def fake_search_text(q: str, *, sources=None):
        seen.append(sources)
        return LookupResult(candidates=[
            _cand("wookieepedia", "X", series="Foo"),
        ])
    async def fake_lookup_full(ident: str, *, sources=None):
        seen.append(sources)
        return LookupResult()
    monkeypatch.setattr("app.services.aggregator.search_text", fake_search_text)
    monkeypatch.setattr("app.services.aggregator.lookup_full", fake_lookup_full)

    await find_candidates_multi(
        series="Foo", title="Bar", sources=["wookieepedia"],
    )
    # `search_text` got called with the user's restricted set.
    assert seen == [{"wookieepedia"}]


@pytest.mark.asyncio
async def test_multi_search_drops_rate_limit_warnings_for_unselected_sources(monkeypatch):
    """If a source the user didn't pick happens to rate-limit, we must
    NOT surface that as a warning — it's noise from a source they
    wouldn't use anyway."""
    async def fake_search_text(q: str, *, sources=None):
        # Pretend the underlying fn (despite getting `sources=...`) still
        # returned a metron rate-limit warning. The aggregator should
        # filter it out.
        return LookupResult(
            candidates=[_cand("wookieepedia", "X", series="Foo")],
            rate_limited=["metron"],
        )
    async def fake_lookup_full(ident: str, *, sources=None):
        return LookupResult()
    monkeypatch.setattr("app.services.aggregator.search_text", fake_search_text)
    monkeypatch.setattr("app.services.aggregator.lookup_full", fake_lookup_full)

    r = await find_candidates_multi(
        series="Foo", title="Bar", sources=["wookieepedia"],
    )
    assert r.rate_limited == []


@pytest.mark.asyncio
async def test_multi_search_ranks_by_year_proximity(monkeypatch):
    async def fake_search_text(q: str, *, sources=None):
        return LookupResult(candidates=[
            _cand("comicvine", "old",  series="Foo", cover_date="1999-01-01"),
            _cand("comicvine", "near", series="Foo", cover_date="2015-06-01"),
            _cand("comicvine", "spot", series="Foo", cover_date="2014-01-01"),
        ])
    async def fake_lookup_full(ident: str, *, sources=None):
        return LookupResult()
    monkeypatch.setattr("app.services.aggregator.search_text", fake_search_text)
    monkeypatch.setattr("app.services.aggregator.lookup_full", fake_lookup_full)

    r = await find_candidates_multi(
        series="Foo", title="Bar", year=2014, year_tolerance=1,
    )
    # Within tolerance "spot" (2014, |Δ|=0) and "near" (2015, |Δ|=1) come
    # first; "old" outside tolerance lands last.
    sids = [c.source_id for c in r.candidates]
    assert sids[0] == "spot"
    assert sids[1] == "near"
    assert sids[-1] == "old"


@pytest.mark.asyncio
async def test_multi_search_dedupes_same_source_id(monkeypatch):
    async def fake_search_text(q: str, *, sources=None):
        return LookupResult(candidates=[
            _cand("comicvine", "1", title="A"),
            _cand("comicvine", "1", title="A duplicate"),
            _cand("comicvine", "2", title="B"),
        ])
    async def fake_lookup_full(ident: str, *, sources=None):
        return LookupResult()
    monkeypatch.setattr("app.services.aggregator.search_text", fake_search_text)
    monkeypatch.setattr("app.services.aggregator.lookup_full", fake_lookup_full)

    r = await find_candidates_multi(series="Foo", title="Bar")
    assert sorted(c.source_id for c in r.candidates) == ["1", "2"]


# ── /resolve page rendering ────────────────────────────────────────────


def test_resolve_page_renders_one_card_per_row():
    csv_text = "Series,Title\nA,A1\nB,B1\nC,C1\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        r = client.get(f"/admin/import/csv/{token}/resolve")
        assert r.status_code == 200
        # Three cards in the rows section. We count by the card id prefix
        # (one per row) rather than `hx-trigger="revealed"` because that
        # string is also in the JS source on the page.
        assert r.text.count('id="lb-row-') == 3
        # Status badge for pending state.
        assert "searching" in r.text
        # Sticky progress footer is rendered EXACTLY ONCE — earlier bug
        # had the row partial OOB-emit it per row, producing N duplicates.
        assert r.text.count('id="lb-import-progress-inner"') == 1


def test_search_endpoint_includes_oob_progress_swap(monkeypatch):
    """When a per-row HTMX swap response comes back, it MUST include the
    sticky-footer OOB block so counts stay current. The initial page
    render does NOT (covered by the dup test above)."""
    async def fake_find(**kw):
        return LookupResult(candidates=[
            _cand("comicvine", "1", title="X", series="Foo"),
        ])
    monkeypatch.setattr(
        "app.services.aggregator.find_candidates_multi", fake_find,
    )

    csv_text = "Series,Title\nFoo,Foo #1\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        rid = _row_id(token, 0)
        r = client.post(f"/admin/import/csv/{token}/rows/{rid}/search")
        # Single OOB block on swap responses too (the page only ever shows
        # one footer at a time).
        assert r.text.count('id="lb-import-progress-inner"') == 1
        assert 'hx-swap-oob="true"' in r.text


def test_search_surfaces_rate_limit_warning_on_partial_results(monkeypatch):
    async def fake_find(**kw):
        return LookupResult(
            candidates=[_cand("comicvine", "1", title="X", series="Foo")],
            rate_limited=["wookieepedia"],
        )
    monkeypatch.setattr(
        "app.services.aggregator.find_candidates_multi", fake_find,
    )

    csv_text = "Series,Title\nFoo,Foo #1\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        rid = _row_id(token, 0)
        r = client.post(f"/admin/import/csv/{token}/rows/{rid}/search")
        # Soft warning rendered as a yellow chip; status is still matched.
        assert "rate-limited" in r.text.lower()
        row = _row_state(rid)
        assert row.status == "matched"
        assert "rate-limited" in (row.error or "").lower()


def test_search_marks_errored_when_all_sources_rate_limited(monkeypatch):
    async def fake_find(**kw):
        return LookupResult(candidates=[], rate_limited=["comicvine", "metron"])
    monkeypatch.setattr(
        "app.services.aggregator.find_candidates_multi", fake_find,
    )

    csv_text = "Series,Title\nFoo,Foo #1\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text, sources=("comicvine", "metron"))
        rid = _row_id(token, 0)
        client.post(f"/admin/import/csv/{token}/rows/{rid}/search")
        row = _row_state(rid)
        assert row.status == "errored"
        assert "rate-limited" in (row.error or "").lower()


def test_resolve_redirects_when_config_not_done():
    """If the user pokes /resolve directly without finishing /config,
    we punt them back to /config."""
    csv_text = "Series,Title\nA,A1\n"
    with _client() as client:
        token = _post_csv(client, csv_text)
        # Map column but skip /config submission.
        client.post(
            f"/admin/import/csv/{token}/map",
            data={"map[series]": "Series", "map[title]": "Title"},
            follow_redirects=False,
        )
        r = client.get(
            f"/admin/import/csv/{token}/resolve", follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"].endswith("/config")


# ── /rows/{id}/search ───────────────────────────────────────────────────


def _row_id(token: str, row_index: int) -> int:
    async def _go():
        async with SessionLocal() as session:
            sess = (await session.exec(
                select(ImportSession).where(ImportSession.token == token)
            )).first()
            row = (await session.exec(
                select(ImportRow)
                .where(ImportRow.session_id == sess.id, ImportRow.row_index == row_index)
            )).first()
            return row.id
    return asyncio.run(_go())


def _row_state(row_id: int) -> ImportRow:
    async def _go():
        async with SessionLocal() as session:
            return await session.get(ImportRow, row_id)
    return asyncio.run(_go())


def test_search_endpoint_marks_matched_when_one_hit(monkeypatch):
    async def fake_find(**kw):
        return LookupResult(candidates=[
            _cand("comicvine", "42", title="The Hit", series="Foo",
                  cover_date="2015-01-01"),
        ])
    monkeypatch.setattr(
        "app.services.aggregator.find_candidates_multi", fake_find,
    )

    csv_text = "Series,Title\nFoo,Bar\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        rid = _row_id(token, 0)
        r = client.post(f"/admin/import/csv/{token}/rows/{rid}/search")
        assert r.status_code == 200
        # Card markup reflects matched state.
        assert "✓ matched" in r.text or "matched" in r.text
        # OOB progress block shipped along with the card.
        assert 'id="lb-import-progress-inner"' in r.text

        row = _row_state(rid)
        assert row.status == "matched"
        assert row.chosen_source == "comicvine"
        assert row.chosen_source_id == "42"
        cands = json.loads(row.candidates)
        assert len(cands) == 1


def test_search_endpoint_marks_multi_for_two_or_more_hits(monkeypatch):
    async def fake_find(**kw):
        return LookupResult(candidates=[
            _cand("comicvine", "1", title="Hit 1", series="Foo"),
            _cand("metron", "2",   title="Hit 2", series="Foo"),
        ])
    monkeypatch.setattr(
        "app.services.aggregator.find_candidates_multi", fake_find,
    )

    csv_text = "Series,Title\nFoo,Bar\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text, sources=("comicvine", "metron"))
        rid = _row_id(token, 0)
        client.post(f"/admin/import/csv/{token}/rows/{rid}/search")
        row = _row_state(rid)
        assert row.status == "multi"
        # Top candidate auto-selected as the default pick.
        assert row.chosen_source == "comicvine"


def test_search_endpoint_marks_not_found_for_zero_hits(monkeypatch):
    async def fake_find(**kw):
        return LookupResult(candidates=[])
    monkeypatch.setattr(
        "app.services.aggregator.find_candidates_multi", fake_find,
    )

    csv_text = "Series,Title\nFoo,Bar\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        rid = _row_id(token, 0)
        client.post(f"/admin/import/csv/{token}/rows/{rid}/search")
        row = _row_state(rid)
        assert row.status == "not_found"


# ── /rows/{id}/pick · /skip · /as-is ────────────────────────────────────


def test_pick_endpoint_records_user_choice(monkeypatch):
    async def fake_find(**kw):
        return LookupResult(candidates=[
            _cand("comicvine", "1", title="Hit 1"),
            _cand("metron",    "2", title="Hit 2"),
        ])
    monkeypatch.setattr(
        "app.services.aggregator.find_candidates_multi", fake_find,
    )

    csv_text = "Series,Title\nFoo,Bar\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text, sources=("comicvine", "metron"))
        rid = _row_id(token, 0)
        client.post(f"/admin/import/csv/{token}/rows/{rid}/search")
        # User overrides the auto-picked default with the second hit.
        client.post(
            f"/admin/import/csv/{token}/rows/{rid}/pick",
            data={"source": "metron", "source_id": "2"},
        )
        row = _row_state(rid)
        assert row.status == "matched"
        assert row.chosen_source == "metron"
        assert row.chosen_source_id == "2"


def test_custom_search_replaces_candidates_with_freeform_query(monkeypatch):
    """When the auto-search returns the wrong thing, the user can type
    a freeform query in the row's search box and re-run."""
    captured: list[dict] = []

    async def fake_find(**kw):
        captured.append(kw)
        if kw.get("custom_query"):
            return LookupResult(candidates=[
                _cand("comicvine", "777", title="The Real Match",
                      series="Right Series"),
            ])
        return LookupResult(candidates=[
            _cand("comicvine", "1", title="Wrong Match", series="Wrong Series"),
        ])
    monkeypatch.setattr(
        "app.services.aggregator.find_candidates_multi", fake_find,
    )

    csv_text = "Series,Title\nFoo,Foo #1\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        rid = _row_id(token, 0)
        client.post(f"/admin/import/csv/{token}/rows/{rid}/search")
        # Initial auto-search produced "Wrong Match".
        row = _row_state(rid)
        assert row.chosen_source_id == "1"

        # Custom search re-runs with the user's query.
        r = client.post(
            f"/admin/import/csv/{token}/rows/{rid}/search-custom",
            data={"q": "the real match"},
        )
        assert r.status_code == 200
        assert "The Real Match" in r.text

        # Aggregator received custom_query="the real match" on the second call.
        assert captured[-1]["custom_query"] == "the real match"
        row = _row_state(rid)
        assert row.chosen_source_id == "777"


def test_custom_search_with_blank_query_is_a_noop(monkeypatch):
    async def fake_find(**kw):  # pragma: no cover — must NOT be called
        raise AssertionError("custom_search must not invoke aggregator on blank")
    monkeypatch.setattr(
        "app.services.aggregator.find_candidates_multi", fake_find,
    )
    csv_text = "Series,Title\nFoo,Foo #1\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        rid = _row_id(token, 0)
        r = client.post(
            f"/admin/import/csv/{token}/rows/{rid}/search-custom",
            data={"q": "   "},
        )
        # Renders the existing card without crashing.
        assert r.status_code == 200


def test_cancel_endpoint_drops_session_and_redirects_to_admin_with_flash():
    csv_text = "Series,Title\nA,A1\nB,B1\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)

        async def _check():
            async with SessionLocal() as session:
                sess = (await session.exec(
                    select(ImportSession).where(ImportSession.token == token)
                )).first()
                if sess is None:
                    return None, 0
                rows = (await session.exec(
                    select(ImportRow).where(ImportRow.session_id == sess.id)
                )).all()
                return sess, len(rows)

        # Sanity: session + rows exist before cancel.
        sess_before, n_before = asyncio.run(_check())
        assert sess_before is not None and n_before == 2

        r = client.post(
            f"/admin/import/csv/{token}/cancel", follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/admin?flash=Import+canceled.#import"

        # Session is gone, and no rows remain that pointed at its id.
        sess_after, n_after = asyncio.run(_check())
        assert sess_after is None
        assert n_after == 0


def test_admin_page_renders_flash_banner_from_query_param():
    with _client() as client:
        r = client.get("/admin", params={"flash": "Import canceled."})
        assert r.status_code == 200
        assert "Import canceled" in r.text


def test_commit_button_disabled_only_for_pending_rows():
    """Errored rows DON'T block the commit button — `commit_session`
    treats them as skipped. Only `pending` (the lazy-loaded card hasn't
    revealed yet) actually gates commit."""
    csv_text = "Series,Title\nA,A1\nB,B1\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        # Force one row into errored state without going through search.
        async def _err_one():
            async with SessionLocal() as session:
                sess = (await session.exec(
                    select(ImportSession).where(ImportSession.token == token)
                )).first()
                rows = (await session.exec(
                    select(ImportRow).where(ImportRow.session_id == sess.id)
                )).all()
                rows[0].status = "errored"
                rows[0].error = "rate limited"
                rows[1].status = "matched"
                rows[1].chosen_source = "comicvine"
                rows[1].chosen_source_id = "1"
                for r in rows:
                    session.add(r)
                await session.commit()
        asyncio.run(_err_one())
        page = client.get(f"/admin/import/csv/{token}/resolve").text
        # The button-style "pointer-events-none opacity-50" combo only
        # appears when the gate fires. (`pointer-events-none` alone is
        # also on the click-through wrapper around the sticky footer
        # — that one's permanent.)
        assert "pointer-events-none opacity-50" not in page
        # Standard "Import N →" label visible (matched=1, errored skipped).
        assert "Import 1 comic →" in page


def test_commit_button_disabled_when_rows_pending():
    csv_text = "Series,Title\nA,A1\nB,B1\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        # Default state has all rows pending — no search has run yet.
        page = client.get(f"/admin/import/csv/{token}/resolve").text
        # Button-specific gate combo (vs. the sticky-footer wrapper's
        # always-on `pointer-events-none`).
        assert "pointer-events-none opacity-50" in page
        assert "Waiting on" in page
        # And the "search remaining" helper is offered.
        assert "search 2 remaining" in page


def test_resolve_reload_shows_candidates_for_already_searched_rows(monkeypatch):
    """Regression: re-rendering /resolve after a row has already been
    searched must keep its candidate list visible. Earlier the resolve
    handler omitted `candidates` from the summary dict, so reloading
    showed status="matched" / "multi" without any picker rows under it."""
    async def fake_find(**kw):
        return LookupResult(candidates=[
            _cand("comicvine", "1", title="Hit 1", series="Foo",
                  cover_url="http://example.com/1.jpg"),
            _cand("metron", "2", title="Hit 2", series="Foo"),
        ])
    monkeypatch.setattr(
        "app.services.aggregator.find_candidates_multi", fake_find,
    )

    csv_text = "Series,Title\nFoo,Bar\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text, sources=("comicvine", "metron"))
        rid = _row_id(token, 0)
        client.post(f"/admin/import/csv/{token}/rows/{rid}/search")

        # Now reload the page — both candidate links should still appear.
        page = client.get(f"/admin/import/csv/{token}/resolve").text
        assert "Hit 1" in page and "Hit 2" in page
        # And the cover should be there too (proves we hydrated full dicts).
        assert "http://example.com/1.jpg" in page


def test_skip_and_as_is_transitions():
    csv_text = "Series,Title\nFoo,Bar\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        rid = _row_id(token, 0)
        client.post(f"/admin/import/csv/{token}/rows/{rid}/skip")
        assert _row_state(rid).status == "skipped"
        client.post(f"/admin/import/csv/{token}/rows/{rid}/as-is")
        assert _row_state(rid).status == "as_is"
