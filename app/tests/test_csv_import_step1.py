"""Step 1 of the CSV import wizard: upload + parser.

Covers:
  * Pure parser (BOM, separators, section rows, off-by-one leading column).
  * Upload endpoint → creates ImportSession + ImportRow rows, redirects to map.
  * Bad input handling (empty file, oversize file).
  * Stub map page renders so users see the wizard hasn't dropped their data.
"""

from __future__ import annotations

import asyncio
import io

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import ImportRow, ImportSession
from app.services.csv_import import parse_csv


def _client() -> TestClient:
    return TestClient(create_app())


# ── Pure parser ──────────────────────────────────────────────────────────


def test_parser_handles_bom_and_simple_csv():
    raw = "﻿Series,Title,Issue\nFoo,Foo #1,1\n".encode("utf-8")
    parsed = parse_csv(raw)
    assert parsed.headers == ["Series", "Title", "Issue"]
    assert parsed.rows == [{"Series": "Foo", "Title": "Foo #1", "Issue": "1"}]
    assert parsed.skipped_section == 0 and parsed.skipped_empty == 0


def test_parser_drops_section_header_rows_and_empty_rows():
    raw = (
        "Fandom,Series,Title\n"
        "Star Wars,,\n"                  # section header (only fandom set)
        "Star Wars,SW Trades,Knights\n"  # real row
        ",,\n"                           # empty
        "Aggretsuko,,\n"                 # section header
        "Aggretsuko,Aggretsuko,#1\n"     # real row
    ).encode("utf-8")
    parsed = parse_csv(raw)
    assert parsed.skipped_section == 2
    assert parsed.skipped_empty == 1
    assert len(parsed.rows) == 2
    titles = [r["Title"] for r in parsed.rows]
    assert "Knights" in titles and "#1" in titles


def test_parser_handles_leading_empty_column_off_by_one():
    """Star Wars Canon CSV in the wild has a leading empty column."""
    raw = ",Fandom,Series,Title\n,Star Wars,SW,My Issue\n,,,\n".encode("utf-8")
    parsed = parse_csv(raw)
    assert parsed.headers == ["Fandom", "Series", "Title"]
    assert parsed.rows == [{"Fandom": "Star Wars", "Series": "SW", "Title": "My Issue"}]


def test_parser_picks_separator_by_count():
    raw = "Series\tTitle\tIssue\nFoo\tBar\t1\n".encode("utf-8")
    parsed = parse_csv(raw)
    assert parsed.separator == "\t"
    assert parsed.rows[0] == {"Series": "Foo", "Title": "Bar", "Issue": "1"}


def test_parser_returns_empty_on_empty_input():
    assert parse_csv(b"").headers == []
    assert parse_csv(b"   \n\n").headers == []


# ── Upload endpoint ─────────────────────────────────────────────────────


def _post_csv(client: TestClient, content: str, filename="test.csv") -> "tuple[int, str]":
    r = client.post(
        "/admin/import/csv",
        files={"file": (filename, io.BytesIO(content.encode("utf-8")), "text/csv")},
        follow_redirects=False,
    )
    return r.status_code, r.headers.get("location", "")


def test_upload_creates_session_and_rows_and_redirects():
    csv_text = "Series,Title,Issue Number\nA,A1,1\nB,B1,2\n"
    with _client() as client:
        status, loc = _post_csv(client, csv_text)
        assert status == 303
        assert loc.startswith("/admin/import/csv/") and loc.endswith("/map")

        # Pull the row count from the DB to verify persistence.
        async def _check():
            async with SessionLocal() as session:
                sess = (await session.exec(
                    select(ImportSession).order_by(ImportSession.id.desc()).limit(1)
                )).first()
                assert sess is not None
                rows = (await session.exec(
                    select(ImportRow).where(ImportRow.session_id == sess.id)
                )).all()
                return sess, rows
        sess, rows = asyncio.run(_check())
        assert sess.state == "map"
        assert sess.filename == "test.csv"
        assert len(rows) == 2
        # Rows preserve original CSV data as JSON for the next step.
        import json
        first = json.loads(rows[0].raw)
        assert first["Series"] == "A"
        assert first["Title"] == "A1"


def test_upload_rejects_empty_csv_with_friendly_error():
    with _client() as client:
        r = client.post(
            "/admin/import/csv",
            files={"file": ("empty.csv", io.BytesIO(b""), "text/csv")},
        )
        assert r.status_code == 400
        # Apostrophes get HTML-entity-encoded by Jinja's autoescape, so
        # match the post-escape form. The substring is unique to the error.
        assert "find any data rows" in r.text


def test_upload_form_is_reachable():
    with _client() as client:
        r = client.get("/admin/import/csv")
        assert r.status_code == 200
        assert "IMPORT FROM CSV" in r.text


def test_admin_page_links_to_import_wizard():
    """Import lives inside the admin hub now — not in the top nav."""
    with _client() as client:
        r = client.get("/admin")
        assert r.status_code == 200
        assert 'href="/admin/import/csv"' in r.text
        assert "IMPORT" in r.text
        # Sub-nav pills should be present.
        for anchor in ("#backup", "#restore", "#export", "#import", "#cleanup"):
            assert f'href="{anchor}"' in r.text


def test_top_nav_does_not_include_import_link():
    with _client() as client:
        r = client.get("/library")
        assert r.status_code == 200
        # Library is one of every page; it always renders the global nav.
        # The top nav must NOT include a direct Import link anymore.
        assert 'href="/import/csv"' not in r.text
        assert 'href="/admin/import/csv">Import</a>' not in r.text


def test_map_page_renders_for_uploaded_session():
    """Sanity check that the redirect from upload lands on a real page,
    not a 404. Detailed mapping behavior is covered in test_csv_import_step2."""
    csv_text = "Series,Title\nFoo,Foo #1\n"
    with _client() as client:
        _post_csv(client, csv_text)
        async def _token():
            async with SessionLocal() as session:
                sess = (await session.exec(
                    select(ImportSession).order_by(ImportSession.id.desc()).limit(1)
                )).first()
                return sess.token
        token = asyncio.run(_token())
        r = client.get(f"/admin/import/csv/{token}/map")
        assert r.status_code == 200
        assert "MAP COLUMNS" in r.text
