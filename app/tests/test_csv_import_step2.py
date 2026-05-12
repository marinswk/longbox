"""Step 2 of the CSV import wizard: column mapping.

Covers:
  * Pure `suggest_mapping()` autosuggester behavior (case/punct insensitivity,
    one-to-one, ordering preserved).
  * GET /admin/import/csv/{token}/map renders the form with autosuggested
    selections + a sample preview seeded with `<script id="lb-import-samples">`.
  * POST persists the user's `column_map` JSON and redirects to /config.
  * Revisiting the GET shows the previously-saved map pre-selected.
"""

from __future__ import annotations

import asyncio
import io
import json

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import ImportSession
from app.services.csv_import import (
    OUR_FIELDS,
    OUR_FIELD_KEYS,
    suggest_mapping,
    translate_format,
)


def _client() -> TestClient:
    return TestClient(create_app())


def _post_csv(client: TestClient, content: str, filename="t.csv") -> str:
    """Upload a CSV, return the wizard token."""
    r = client.post(
        "/admin/import/csv",
        files={"file": (filename, io.BytesIO(content.encode("utf-8")), "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return r.headers["location"].split("/")[-2]  # ".../csv/<token>/map"


# ── Pure autosuggest ────────────────────────────────────────────────────


def test_autosuggest_handles_user_csv_headers():
    headers = ["Fandom", "SeriesName", "Series Year", "Title", "Issue Number",
               "Type", "Publisher", "Collected Issues", "Variant"]
    suggested = suggest_mapping(headers)
    assert suggested["series"] == "SeriesName"
    assert suggested["title"] == "Title"
    assert suggested["issue_number"] == "Issue Number"
    assert suggested["year"] == "Series Year"
    assert suggested["publisher"] == "Publisher"
    assert suggested["format"] == "Type"
    assert suggested["collected_issues"] == "Collected Issues"
    assert suggested["variant"] == "Variant"
    assert suggested["fandom"] == "Fandom"


def test_autosuggest_handles_punctuation_and_case_variants():
    # "ISBN-13" / "isbn_13" / "ISBN 13" should all hit isbn_13.
    suggested = suggest_mapping(["ISBN-13", "Title"])
    assert suggested["isbn_13"] == "ISBN-13"
    suggested = suggest_mapping(["isbn_13", "Title"])
    assert suggested["isbn_13"] == "isbn_13"


def test_autosuggest_one_to_one_no_double_claim():
    """If a CSV has a single ambiguous column, only one target field gets
    it — the rest fall through to no suggestion."""
    headers = ["Title"]
    suggested = suggest_mapping(headers)
    assert suggested["title"] == "Title"
    # No ghost mappings:
    assert "series" not in suggested
    assert "issue_number" not in suggested


def test_autosuggest_unknown_headers_are_dropped():
    suggested = suggest_mapping(["Wat", "FooBar", "Notes"])
    assert suggested == {}


def test_translate_format_normalizes_user_csv_enum_values():
    assert translate_format("TPB") == "trade paperback"
    assert translate_format("HC") == "hardcover"
    assert translate_format("OMNIBUS") == "omnibus"
    assert translate_format("SINGLE_ISSUE") == "single issue"
    assert translate_format("OGN") == "graphic novel"
    # Unknown enums passed through (lowercased) — better than dropping.
    assert translate_format("Hardcover Deluxe") == "hardcover deluxe"
    assert translate_format("") is None
    assert translate_format(None) is None


# ── GET /map ────────────────────────────────────────────────────────────


def test_get_map_renders_form_with_autosuggested_selections():
    csv_text = ("Fandom,SeriesName,Series Year,Title,Issue Number,Type,Publisher\n"
                "Star Wars,SW Trades,2015,Skywalker Strikes,1,TPB,Marvel\n"
                "Star Wars,SW Trades,2015,Showdown,2,TPB,Marvel\n")
    with _client() as client:
        token = _post_csv(client, csv_text)
        r = client.get(f"/admin/import/csv/{token}/map")
        assert r.status_code == 200
        # Form lists every target field with the right name attribute.
        for tf in OUR_FIELDS:
            assert f'name="map[{tf.key}]"' in r.text
        # Autosuggest should mark `Series` as selected for the `series` target.
        # (The HTML is `selected` next to the chosen <option>.)
        assert '<option value="SeriesName" selected>' in r.text
        assert '<option value="Title" selected>' in r.text
        # Sample preview JSON is embedded on the page.
        assert 'id="lb-import-samples"' in r.text


def test_get_map_shows_saved_mapping_when_revisiting():
    csv_text = "A,B,C\n1,2,3\n"
    with _client() as client:
        token = _post_csv(client, csv_text)
        # Save a custom mapping that doesn't match autosuggest.
        client.post(
            f"/admin/import/csv/{token}/map",
            data={
                "map[series]": "A",
                "map[title]": "B",
                "map[issue_number]": "C",
            },
            follow_redirects=False,
        )
        # Revisit the form — saved selections should be pre-selected.
        r = client.get(f"/admin/import/csv/{token}/map")
        assert '<option value="A" selected>' in r.text
        assert '<option value="B" selected>' in r.text
        assert '<option value="C" selected>' in r.text


def test_get_map_404_for_unknown_token():
    with _client() as client:
        r = client.get("/admin/import/csv/no-such-token/map")
        assert r.status_code == 404


# ── POST /map ───────────────────────────────────────────────────────────


def test_post_map_persists_column_map_and_redirects_to_config():
    csv_text = "Series,Title,Issue\nFoo,Foo #1,1\n"
    with _client() as client:
        token = _post_csv(client, csv_text)
        r = client.post(
            f"/admin/import/csv/{token}/map",
            data={
                "map[series]": "Series",
                "map[title]": "Title",
                "map[issue_number]": "Issue",
                "map[publisher]": "",   # unmapped — must NOT appear in JSON
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == f"/admin/import/csv/{token}/config"

        async def _check():
            async with SessionLocal() as session:
                sess = (await session.exec(
                    select(ImportSession).where(ImportSession.token == token)
                )).first()
                return sess
        sess = asyncio.run(_check())
        assert sess.state == "config"
        column_map = json.loads(sess.column_map)
        assert column_map == {
            "series": "Series",
            "title": "Title",
            "issue_number": "Issue",
        }
        assert "publisher" not in column_map  # blank values dropped


def test_post_map_with_no_selections_still_succeeds():
    """Edge case: user submits the form without picking anything. The
    wizard accepts it (the next step will tell them they need at least
    one searchable field)."""
    csv_text = "X,Y\n1,2\n"
    with _client() as client:
        token = _post_csv(client, csv_text)
        r = client.post(
            f"/admin/import/csv/{token}/map",
            data={f"map[{k}]": "" for k in OUR_FIELD_KEYS},
            follow_redirects=False,
        )
        assert r.status_code == 303

        async def _check():
            async with SessionLocal() as session:
                sess = (await session.exec(
                    select(ImportSession).where(ImportSession.token == token)
                )).first()
                return sess
        sess = asyncio.run(_check())
        assert sess.column_map == "{}"
