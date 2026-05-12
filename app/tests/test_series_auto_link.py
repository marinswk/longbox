"""One-click "pull issues automatically" on the series detail page.

POST /series/{id}/auto-link derives source + source_id from a child
comic + the series name, then runs the same refresh path that the
manual form does. The button is what new users (who shouldn't have to
know about Wookieepedia article titles) tap to backfill a legacy
series in a single click.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, Series


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "AL Pub", "series": "AL Series"}
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


def _series(series_id: int) -> Series:
    async def _go():
        async with SessionLocal() as session:
            return await session.get(Series, series_id)
    return asyncio.run(_go())


def _set_comic_source(comic_id: int, source: str, source_id: str) -> None:
    async def _go():
        async with SessionLocal() as session:
            c = await session.get(Comic, comic_id)
            c.source = source
            c.source_id = source_id
            session.add(c)
            await session.commit()
    asyncio.run(_go())


def test_auto_link_uses_existing_series_source_when_set():
    """When the Series row already carries source + source_id, the
    one-click button just re-runs the refresh against them. No
    sniffing needed."""
    fake_issues = ["Saga #1", "Saga #2"]

    async def fake(title: str) -> list[str]:
        return fake_issues

    # Patch the _FETCHERS dict in the router: the module imports the
    # fetcher into the dict at load time, so monkey-patching the
    # underlying function on the service module wouldn't reach it.
    with patch.dict("app.routers.series._FETCHERS", {"wookieepedia": fake}):
        with _client() as client:
            cid = _save(client, title="AL preset",
                        isbn_13="9789000002001",
                        series="AL Preset Series")
            comic = _comic(cid)

            # Stamp the series with a source manually (simulating a
            # series that was already linked at some point).
            async def _seed():
                async with SessionLocal() as session:
                    s = await session.get(Series, comic.series_id)
                    s.source = "wookieepedia"
                    s.source_id = "AL Preset Series"
                    session.add(s)
                    await session.commit()
            asyncio.run(_seed())

            r = client.post(f"/series/{comic.series_id}/auto-link")
            assert r.status_code == 204
            series = _series(comic.series_id)
            assert series.expected_issues == "Saga #1\nSaga #2"


def test_auto_link_sniffs_source_from_child_comic_for_wookieepedia():
    """Legacy path: Series has no source yet. We look at the child
    comics, see they came from Wookieepedia, and use the SERIES NAME
    as the upstream article title — works for the typical case where
    the series name matches a Wookieepedia article."""
    fake_issues = ["Knights #1", "Knights #2", "Knights #3"]

    async def fake(title: str) -> list[str]:
        # The endpoint should pass the series name through.
        assert title == "AL-Wookie-2002"
        return fake_issues

    # Patch the _FETCHERS dict in the router: the module imports the
    # fetcher into the dict at load time, so monkey-patching the
    # underlying function on the service module wouldn't reach it.
    with patch.dict("app.routers.series._FETCHERS", {"wookieepedia": fake}):
        with _client() as client:
            cid = _save(client, title="ALW issue 1",
                        isbn_13="9789000002002",
                        series="AL-Wookie-2002",
                        publisher="AL Pub")
            comic = _comic(cid)

            # Comics from the live save path don't always have a source
            # set (depends on the candidate); pretend this one came
            # from Wookieepedia.
            _set_comic_source(cid, "wookieepedia", "ALW issue 1")
            # Clear the series.source to simulate the legacy case.
            async def _clear_series():
                async with SessionLocal() as session:
                    s = await session.get(Series, comic.series_id)
                    s.source = None
                    s.source_id = None
                    s.expected_issues = None
                    session.add(s)
                    await session.commit()
            asyncio.run(_clear_series())

            r = client.post(f"/series/{comic.series_id}/auto-link")
            assert r.status_code == 204
            series = _series(comic.series_id)
            assert series.source == "wookieepedia"
            assert series.source_id == "AL-Wookie-2002"
            assert "Knights #1" in series.expected_issues


def test_auto_link_returns_422_when_no_comic_has_a_source():
    """If none of the comics in this series carry a source there's
    nothing to sniff — surface a 422 so the UI can fall back to the
    manual form."""
    with _client() as client:
        cid = _save(client, title="AL-Bare",
                    isbn_13="9789000002003",
                    series="AL-Bare-Series",
                    publisher="AL Pub")
        comic = _comic(cid)
        # Make sure no source/source_id on the comic.
        async def _strip():
            async with SessionLocal() as session:
                c = await session.get(Comic, cid)
                c.source = None
                c.source_id = None
                session.add(c)
                s = await session.get(Series, comic.series_id)
                s.source = None
                s.source_id = None
                session.add(s)
                await session.commit()
        asyncio.run(_strip())

        r = client.post(f"/series/{comic.series_id}/auto-link")
        assert r.status_code == 422
        assert "auto-detect" in r.json()["detail"].lower()


def test_auto_link_returns_502_when_upstream_has_no_issues():
    """The series name doesn't always match a wiki article. When the
    fetcher returns an empty list the endpoint should bubble up a 502
    so the user can drop down to the manual form and try a different
    article title."""
    async def fake(title: str) -> list[str]:
        return []

    # Patch the _FETCHERS dict in the router: the module imports the
    # fetcher into the dict at load time, so monkey-patching the
    # underlying function on the service module wouldn't reach it.
    with patch.dict("app.routers.series._FETCHERS", {"wookieepedia": fake}):
        with _client() as client:
            cid = _save(client, title="AL-Empty issue 1",
                        isbn_13="9789000002004",
                        series="AL-Empty-2004",
                        publisher="AL Pub")
            comic = _comic(cid)
            _set_comic_source(cid, "wookieepedia", "AL-Empty issue 1")
            async def _clear_series():
                async with SessionLocal() as session:
                    s = await session.get(Series, comic.series_id)
                    s.source = None
                    s.source_id = None
                    session.add(s)
                    await session.commit()
            asyncio.run(_clear_series())

            r = client.post(f"/series/{comic.series_id}/auto-link")
            assert r.status_code == 502


def test_series_page_renders_one_click_button_when_source_known():
    """Cosmetic: when the series knows its upstream we hide the inputs
    and show a single Pull-issues button instead of the form."""
    with _client() as client:
        cid = _save(client, title="AL-Page issue",
                    isbn_13="9789000002005",
                    series="AL-Page-Series",
                    publisher="AL Pub")
        comic = _comic(cid)
        async def _seed():
            async with SessionLocal() as session:
                s = await session.get(Series, comic.series_id)
                s.source = "wookieepedia"
                s.source_id = "AL-Page-Series"
                session.add(s)
                await session.commit()
        asyncio.run(_seed())

        r = client.get(f"/series/{comic.series_id}")
        assert r.status_code == 200
        assert "↻ Pull issues" in r.text
        # The change-source disclosure exists but is collapsed.
        assert "Change source / ID" in r.text


def test_series_page_renders_auto_link_button_when_source_unknown():
    """Inverse: a series with no source linked yet shows the auto-link
    one-click button + a 'Manual: specify source + ID' fallback."""
    with _client() as client:
        cid = _save(client, title="AL-Cold issue",
                    isbn_13="9789000002006",
                    series="AL-Cold-Series",
                    publisher="AL Pub")
        comic = _comic(cid)
        async def _clear():
            async with SessionLocal() as session:
                s = await session.get(Series, comic.series_id)
                s.source = None
                s.source_id = None
                session.add(s)
                await session.commit()
        asyncio.run(_clear())

        r = client.get(f"/series/{comic.series_id}")
        assert r.status_code == 200
        assert "Pull issues automatically" in r.text
        assert f'hx-post="/series/{comic.series_id}/auto-link"' in r.text
        assert "Manual: specify source + ID" in r.text
