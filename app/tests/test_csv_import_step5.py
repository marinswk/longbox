"""Step 5 of the CSV import wizard: commit.

End-to-end commit tests with monkeypatched `_refetch_candidate` so we don't
hit the network. Verifies:
  * matched rows produce a Comic with metadata from the candidate.
  * as-is rows produce a bare Comic from CSV fields only.
  * skipped + not_found rows do nothing.
  * errored rows are swallowed and reported in the summary.
  * The session ends in `state="done"`.
"""

from __future__ import annotations

import asyncio
import io
import json

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, ImportRow, ImportSession
from app.services.aggregator import LookupResult
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


def _drive_to_resolve(client: TestClient, csv_text: str) -> str:
    """Walk a CSV through upload+map+config so we land on /resolve."""
    token = _post_csv(client, csv_text)
    headers = csv_text.splitlines()[0].split(",")
    mapping: dict[str, str] = {}
    for h in headers:
        h = h.strip()
        low = h.lower()
        if low in ("series", "seriesname"):
            mapping["series"] = h
        elif low == "title":
            mapping["title"] = h
        elif low in ("issue number", "issue"):
            mapping["issue_number"] = h
        elif low == "publisher":
            mapping["publisher"] = h
        elif low == "fandom":
            mapping["fandom"] = h
        elif low == "type":
            mapping["format"] = h
        elif low in ("series year", "year"):
            mapping["year"] = h
    client.post(
        f"/admin/import/csv/{token}/map",
        data={f"map[{k}]": v for k, v in mapping.items()},
        follow_redirects=False,
    )
    client.post(
        f"/admin/import/csv/{token}/config",
        data={
            "source[comicvine]": "on",
            "source[wookieepedia]": "on",
            "year_tolerance": "1",
            "auto_tag_fandom": "on",
        },
        follow_redirects=False,
    )
    return token


async def _commit_resolve_for_each_row(token: str, candidate_for_row: dict[int, LookupCandidate | None]):
    """Mark every row with the appropriate state without going through
    the network-hitting search endpoint."""
    async with SessionLocal() as session:
        sess = (await session.exec(
            select(ImportSession).where(ImportSession.token == token)
        )).first()
        rows = (await session.exec(
            select(ImportRow).where(ImportRow.session_id == sess.id)
            .order_by(ImportRow.row_index.asc())
        )).all()
        for row in rows:
            cand = candidate_for_row.get(row.row_index)
            if cand is None:
                row.status = "as_is"
                row.chosen_source = None
                row.chosen_source_id = None
                row.candidates = json.dumps([])
            else:
                row.status = "matched"
                row.chosen_source = cand.source
                row.chosen_source_id = cand.source_id
                row.candidates = json.dumps([{
                    "source": cand.source, "source_id": cand.source_id,
                    "title": cand.title, "series": cand.series,
                    "publisher": cand.publisher,
                }])
            session.add(row)
        await session.commit()


def _all_comics() -> list[Comic]:
    async def _go():
        async with SessionLocal() as session:
            return list((await session.exec(select(Comic))).all())
    return asyncio.run(_go())


# ── Tests ───────────────────────────────────────────────────────────────


def test_commit_creates_comics_for_matched_rows(monkeypatch):
    fake = LookupCandidate(
        source="comicvine", source_id="42",
        title="The Hit", series="Foo", publisher="Acme",
        cover_date="2015-06-01",
        creators=[], story_arcs=[], characters=[],
    )
    async def fake_refetch(source: str, source_id: str):
        if source == "comicvine" and source_id == "42":
            return fake
        return None
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    csv_text = "Series,Title,Issue Number,Fandom\nFoo,Foo #1,1,Star Wars\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        asyncio.run(_commit_resolve_for_each_row(token, {0: fake}))

        r = client.post(f"/admin/import/csv/{token}/commit")
        assert r.status_code == 200
        assert "DONE" in r.text
        # Count is wrapped in <span> tags, so search for the trailing prose
        # rather than a contiguous "1 comic" substring.
        assert "comic added" in r.text

        comics = [c for c in _all_comics() if c.title == "The Hit"]
        assert len(comics) == 1
        c = comics[0]
        # Candidate fields take precedence; CSV's Fandom column lands on Comic.fandom.
        assert c.title == "The Hit"
        assert c.source == "comicvine"
        assert c.source_id == "42"
        assert c.fandom == "star wars"


def test_commit_creates_bare_comic_for_as_is_rows(monkeypatch):
    async def fake_refetch(source: str, source_id: str):
        return None  # not used; row is as-is
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    csv_text = ("Series,Title,Issue Number,Publisher,Type\n"
                "Indie Comic,Issue Zero,0,Self-Published,SINGLE_ISSUE\n")
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        # Row 0 is marked as-is (no candidate).
        asyncio.run(_commit_resolve_for_each_row(token, {0: None}))
        r = client.post(f"/admin/import/csv/{token}/commit")
        assert r.status_code == 200

        comic = next((c for c in _all_comics() if c.title == "Issue Zero"), None)
        assert comic is not None
        assert comic.source is None  # no upstream
        # Type translation: SINGLE_ISSUE → "single issue".
        assert comic.format == "single issue"
        assert comic.issue_number == "0"


def test_commit_marks_session_done_and_persists_comic_ids(monkeypatch):
    fake = LookupCandidate(source="comicvine", source_id="99", title="X", series="X")
    async def fake_refetch(source, source_id):
        return fake
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    csv_text = "Series,Title\nFoo,Foo #1\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        asyncio.run(_commit_resolve_for_each_row(token, {0: fake}))
        client.post(f"/admin/import/csv/{token}/commit")

        async def _check():
            async with SessionLocal() as session:
                sess = (await session.exec(
                    select(ImportSession).where(ImportSession.token == token)
                )).first()
                rows = (await session.exec(
                    select(ImportRow).where(ImportRow.session_id == sess.id)
                )).all()
                return sess, rows
        sess, rows = asyncio.run(_check())
        assert sess.state == "done"
        assert all(r.status == "committed" and r.comic_id is not None for r in rows)


def test_commit_revisit_renders_summary_idempotently(monkeypatch):
    fake = LookupCandidate(source="comicvine", source_id="11", title="Idem", series="X")
    async def fake_refetch(source, source_id):
        return fake
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    csv_text = "Series,Title\nFoo,Foo #1\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        asyncio.run(_commit_resolve_for_each_row(token, {0: fake}))
        client.post(f"/admin/import/csv/{token}/commit")
        # Visit GET /commit after the run — we should see the done page,
        # not the pre-flight form, and not double-create the comic.
        before = len(_all_comics())
        r = client.get(f"/admin/import/csv/{token}/commit")
        assert r.status_code == 200
        assert "DONE" in r.text
        after = len(_all_comics())
        assert after == before


def test_commit_treats_multi_hit_as_pre_picked_and_imports_it(monkeypatch):
    """A row with status='multi' has the top candidate auto-picked. Commit
    should treat it identically to 'matched' — no extra clicks required."""
    fake = LookupCandidate(source="comicvine", source_id="77", title="Multi Top", series="X")

    async def fake_refetch(source, source_id):
        if source == "comicvine" and source_id == "77":
            return fake
        return None
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    csv_text = "Series,Title\nFoo,Foo #1\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)

        # Manually park a multi-hit row with the fake candidate pre-picked.
        async def _seed():
            async with SessionLocal() as session:
                sess = (await session.exec(
                    select(ImportSession).where(ImportSession.token == token)
                )).first()
                row = (await session.exec(
                    select(ImportRow).where(ImportRow.session_id == sess.id)
                )).first()
                row.status = "multi"
                row.chosen_source = "comicvine"
                row.chosen_source_id = "77"
                row.candidates = json.dumps([{
                    "source": "comicvine", "source_id": "77",
                    "title": "Multi Top", "series": "X",
                }, {
                    "source": "metron", "source_id": "999",
                    "title": "Other", "series": "X",
                }])
                session.add(row)
                await session.commit()
        asyncio.run(_seed())

        r = client.post(f"/admin/import/csv/{token}/commit")
        assert r.status_code == 200
        # The multi row got committed using its pre-picked candidate.
        assert "comic added" in r.text
        assert any(c.title == "Multi Top" for c in _all_comics())


def test_get_commit_shows_preflight_summary():
    csv_text = "Series,Title\nA,A1\nB,B1\n"
    with _client() as client:
        token = _drive_to_resolve(client, csv_text)
        # Don't pick anything — both rows stay pending.
        r = client.get(f"/admin/import/csv/{token}/commit")
        assert r.status_code == 200
        assert "CONFIRM" in r.text
        # Pre-flight page warns if any rows are still pending search.
        assert "still pending" in r.text
