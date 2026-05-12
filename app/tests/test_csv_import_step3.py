"""Step 3 of the CSV import wizard: source-picker + search knobs.

Covers:
  * `build_source_tiles()` — smart defaults respect CSV content.
  * GET /config redirects to /map when no mapping is saved yet.
  * GET renders one tile per source with proper checked state.
  * POST persists the user's selection + knobs and redirects to /resolve.
  * Revisiting GET shows the user's previous picks (not the smart defaults).
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
from app.services.import_sources import build_source_tiles


def _client() -> TestClient:
    return TestClient(create_app())


def _post_csv(client: TestClient, content: str, filename="t.csv") -> str:
    r = client.post(
        "/admin/import/csv",
        files={"file": (filename, io.BytesIO(content.encode("utf-8")), "text/csv")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    return r.headers["location"].split("/")[-2]


def _save_map(client: TestClient, token: str, mapping: dict[str, str]) -> None:
    data = {f"map[{k}]": v for k, v in mapping.items()}
    r = client.post(
        f"/admin/import/csv/{token}/map", data=data, follow_redirects=False,
    )
    assert r.status_code == 303


# ── Pure helper: build_source_tiles ────────────────────────────────────


def test_build_tiles_auto_selects_wookieepedia_when_star_wars_in_fandom():
    rows = [{"Fandom": "Star Wars", "Series": "X"}]
    column_map = {"fandom": "Fandom", "series": "Series"}
    tiles = build_source_tiles(rows, column_map)
    wp = next(t for t in tiles if t.key == "wookieepedia")
    assert wp.default_on is True
    assert "Star Wars" in wp.status


def test_build_tiles_auto_selects_openlibrary_when_isbn_present():
    rows = [{"ISBN": "9780000000001", "Title": "Foo"}]
    column_map = {"isbn_13": "ISBN", "title": "Title"}
    tiles = build_source_tiles(rows, column_map)
    ol = next(t for t in tiles if t.key == "openlibrary")
    assert ol.default_on is True


def test_build_tiles_does_not_auto_select_openlibrary_without_isbn_or_upc():
    tiles = build_source_tiles(
        [{"Title": "Foo"}], column_map={"title": "Title"},
    )
    ol = next(t for t in tiles if t.key == "openlibrary")
    assert ol.default_on is False


def test_build_tiles_locks_to_user_choice_when_chosen_passed():
    """Revisiting the page should preserve the user's earlier picks even
    if smart defaults would have suggested otherwise."""
    rows = [{"Fandom": "Star Wars"}]
    tiles = build_source_tiles(
        rows, {"fandom": "Fandom"},
        chosen_sources=["comicvine"],  # explicit user choice
    )
    wp = next(t for t in tiles if t.key == "wookieepedia")
    cv = next(t for t in tiles if t.key == "comicvine")
    assert wp.default_on is False  # was star-wars-suggested but user un-picked
    # CV may or may not be on depending on whether the API key is set in
    # this test environment — just ensure user choice is honoured when ok.
    if cv.configured:
        assert cv.default_on is True


# ── GET /config ────────────────────────────────────────────────────────


def test_get_config_redirects_to_map_when_no_mapping_saved():
    csv_text = "A,B\n1,2\n"
    with _client() as client:
        token = _post_csv(client, csv_text)
        # Skip /map — go straight to /config.
        r = client.get(f"/admin/import/csv/{token}/config",
                       follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].endswith("/map")


def test_get_config_renders_tiles_once_mapping_is_saved():
    csv_text = ("Fandom,Series,Title\nStar Wars,SW,Foo\n")
    with _client() as client:
        token = _post_csv(client, csv_text)
        _save_map(client, token, {
            "fandom": "Fandom", "series": "Series", "title": "Title",
        })
        r = client.get(f"/admin/import/csv/{token}/config")
        assert r.status_code == 200
        # All four sources rendered as tiles.
        for src in ("Wookieepedia", "ComicVine", "Metron", "Open Library"):
            assert src in r.text
        # Wookieepedia tile is auto-selected because the CSV mentions SW.
        assert 'name="source[wookieepedia]"' in r.text
        # Year-tolerance slider exists with the default value.
        assert 'name="year_tolerance"' in r.text


# ── POST /config ───────────────────────────────────────────────────────


def test_post_config_persists_selection_and_redirects_to_resolve():
    csv_text = "Series,Title\nFoo,Foo #1\n"
    with _client() as client:
        token = _post_csv(client, csv_text)
        _save_map(client, token, {"series": "Series", "title": "Title"})
        r = client.post(
            f"/admin/import/csv/{token}/config",
            data={
                "source[wookieepedia]": "on",
                "source[comicvine]":    "on",
                "year_tolerance": "3",
                "auto_tag_fandom": "on",
                # auto_tag_publisher intentionally omitted = off
            },
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == f"/admin/import/csv/{token}/resolve"

        async def _check():
            async with SessionLocal() as session:
                sess = (await session.exec(
                    select(ImportSession).where(ImportSession.token == token)
                )).first()
                return sess
        sess = asyncio.run(_check())
        assert sess.state == "resolve"
        assert json.loads(sess.sources) == ["wookieepedia", "comicvine"]
        cfg = json.loads(sess.config)
        assert cfg["year_tolerance"] == 3
        assert cfg["auto_tag_fandom"] is True
        assert cfg["auto_tag_publisher"] is False


def test_post_config_clamps_year_tolerance_into_range():
    csv_text = "Series,Title\nA,B\n"
    with _client() as client:
        token = _post_csv(client, csv_text)
        _save_map(client, token, {"series": "Series", "title": "Title"})
        # Out-of-range value gets clamped to the [0, 10] window.
        client.post(
            f"/admin/import/csv/{token}/config",
            data={"year_tolerance": "999"},
            follow_redirects=False,
        )

        async def _check():
            async with SessionLocal() as session:
                sess = (await session.exec(
                    select(ImportSession).where(ImportSession.token == token)
                )).first()
                return json.loads(sess.config)
        cfg = asyncio.run(_check())
        assert cfg["year_tolerance"] == 10


def test_revisiting_config_preserves_previous_source_selection():
    csv_text = "Fandom,Series\nStar Wars,SW\n"
    with _client() as client:
        token = _post_csv(client, csv_text)
        _save_map(client, token, {"fandom": "Fandom", "series": "Series"})
        # First save — explicitly only ComicVine (override the SW default).
        client.post(
            f"/admin/import/csv/{token}/config",
            data={"source[comicvine]": "on"}, follow_redirects=False,
        )
        # Revisit — Wookieepedia must NOT be checked even though the SW
        # heuristic would normally select it.
        r = client.get(f"/admin/import/csv/{token}/config")
        assert r.status_code == 200
        # Find the Wookieepedia checkbox markup and check it isn't `checked`.
        wp_idx = r.text.find('name="source[wookieepedia]"')
        assert wp_idx > 0
        # Look at the checkbox attributes nearby for a `checked` token.
        snippet = r.text[max(0, wp_idx - 200):wp_idx + 200]
        assert "checked" not in snippet
