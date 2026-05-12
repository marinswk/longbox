"""CSV template + round-trippable library export.

* `/admin/import/csv/template` returns an empty CSV with the canonical
  header — no data rows.
* `/admin/import/csv/export-library` returns one row per Comic using the
  same header, so it can be re-uploaded through the import wizard.
* End-to-end round-trip: export → upload → autosuggest matches every
  column onto the right target field.
"""

from __future__ import annotations

import asyncio
import csv as _csv
import io

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic
from app.services.csv_import import canonical_csv_headers, suggest_mapping


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "Test Publisher", "series": "Test Series"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


# ── Canonical header ───────────────────────────────────────────────────


def test_canonical_headers_match_target_fields():
    headers = canonical_csv_headers()
    # Every header autosuggest-matches its own target field — tautological
    # but catches accidental drift between OUR_FIELDS labels and aliases.
    suggested = suggest_mapping(headers)
    # Each header should map to exactly one target.
    assert sorted(suggested.values()) == sorted(headers)


# ── /admin/import/csv/template ─────────────────────────────────────────


def test_template_endpoint_returns_empty_csv_with_header():
    with _client() as client:
        r = client.get("/admin/import/csv/template")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "longbox-import-template.csv" in r.headers["content-disposition"]

        rows = list(_csv.reader(io.StringIO(r.text)))
        assert len(rows) == 1                       # header only
        assert rows[0] == canonical_csv_headers()


# ── /admin/import/csv/export-library ───────────────────────────────────


def test_export_library_emits_one_row_per_comic_with_canonical_header():
    with _client() as client:
        cid = _save(client, title="RT Comic", isbn_13="9795000000001",
                    series="RT Series", publisher="RT Pub", fandom="aggretsuko")
        # Set a few extras directly so we can verify they round-trip.
        async def _seed():
            async with SessionLocal() as session:
                c = await session.get(Comic, cid)
                c.format = "trade paperback"
                c.collected_issues = "Foo 1\nFoo 2"
                c.variant = "1A"
                session.add(c)
                await session.commit()
        asyncio.run(_seed())

        r = client.get("/admin/import/csv/export-library")
        assert r.status_code == 200
        rows = list(_csv.DictReader(io.StringIO(r.text)))
        target = next((row for row in rows if row["Title"] == "RT Comic"), None)
        assert target is not None
        assert target["Series"] == "RT Series"
        assert target["Publisher"] == "RT Pub"
        assert target["Fandom"] == "aggretsuko"
        assert target["Type / format"] == "trade paperback"
        assert target["ISBN-13"] == "9795000000001"
        assert "Foo 1" in target["Collected issues"]
        assert target["Variant"] == "1A"


def test_round_trip_export_upload_autosuggests_every_column():
    """Export the library, re-upload it. The autosuggester should map all
    canonical headers onto target fields without any user intervention."""
    with _client() as client:
        _save(client, title="Loop A", isbn_13="9795000000101",
              series="Loop Series", publisher="Loop Pub", fandom="star wars")
        _save(client, title="Loop B", isbn_13="9795000000102",
              series="Loop Series", publisher="Loop Pub", fandom="star wars")

        # Pull the export.
        export = client.get("/admin/import/csv/export-library").text
        # Re-upload.
        r = client.post(
            "/admin/import/csv",
            files={"file": ("re.csv", io.BytesIO(export.encode("utf-8")), "text/csv")},
            follow_redirects=False,
        )
        assert r.status_code == 303
        token = r.headers["location"].split("/")[-2]
        # Visit the map page — every canonical header should be selected.
        page = client.get(f"/admin/import/csv/{token}/map").text
        for header in canonical_csv_headers():
            assert f'<option value="{header}" selected>' in page, (
                f"header {header!r} did not auto-select on re-upload"
            )
