"""Missing-comics pages + the canon-index crawl.

Covers the pure diff (`compute_missing`), the rendered /missing pages,
and an end-to-end crawl against a mocked Wookieepedia category tree.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import respx
from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, MetadataCache
from app.services import canon_index as ci


def _client() -> TestClient:
    return TestClient(create_app())


def _reset() -> None:
    ci._progress = ci.CrawlProgress()
    ci._task = None


# ---------------------------------------------------------------------------
# compute_missing — pure diff
# ---------------------------------------------------------------------------


def test_compute_missing_splits_owned_and_missing_issues():
    index = {
        "built_at": "2026-01-01",
        "issues": [
            ["Darth Vader (2020) 1", "Star Wars: Darth Vader (2020)"],
            ["Darth Vader (2020) 2", "Star Wars: Darth Vader (2020)"],
            ["Darth Vader (2020) 3", "Star Wars: Darth Vader (2020)"],
        ],
        "tpbs": [],
    }
    comics = [
        # A single issue owned outright.
        Comic(title="DV 1", source="wookieepedia",
              source_id="Darth Vader (2020) 1"),
        # A trade that collects issue 2.
        Comic(title="DV Vol. 1", collected_issues="Darth Vader (2020) 2"),
    ]
    out = ci.compute_missing(index, comics)
    iss = out["issues"]
    assert iss["total"] == 3
    assert iss["owned"] == 2
    assert iss["missing"] == 1
    assert len(iss["groups"]) == 1
    assert iss["groups"][0]["missing"] == ["Darth Vader (2020) 3"]


def test_compute_missing_is_disambiguator_tolerant():
    """A story collected via a redirect title still counts as owned."""
    index = {
        "issues": [["Tall Tales", "Star Wars: Doctor Aphra (2020)"]],
        "tpbs": [],
    }
    comics = [
        Comic(title="Aphra TPB",
              collected_issues="Tall Tales (Revelations) (Revelations (2023) 1)"),
    ]
    out = ci.compute_missing(index, comics)
    assert out["issues"]["missing"] == 0


def test_compute_missing_tpbs_owned_by_source_id():
    index = {
        "issues": [],
        "tpbs": [
            ["Star Wars: Darth Vader Vol. 1", "Star Wars: Darth Vader (2020)"],
            ["Star Wars: Darth Vader Vol. 2", "Star Wars: Darth Vader (2020)"],
        ],
    }
    comics = [
        Comic(title="have it", source="wookieepedia",
              source_id="Star Wars: Darth Vader Vol. 1"),
    ]
    out = ci.compute_missing(index, comics)
    assert out["tpbs"]["owned"] == 1
    assert out["tpbs"]["groups"][0]["missing"] == ["Star Wars: Darth Vader Vol. 2"]


def test_compute_missing_natural_sorts_issue_numbers():
    index = {
        "issues": [
            ["Star Wars 10", "S"], ["Star Wars 2", "S"], ["Star Wars 1", "S"],
        ],
        "tpbs": [],
    }
    out = ci.compute_missing(index, [])
    assert out["issues"]["groups"][0]["missing"] == [
        "Star Wars 1", "Star Wars 2", "Star Wars 10",
    ]


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


def _seed_index() -> None:
    async def _go() -> None:
        async with SessionLocal() as session:
            payload = {
                "built_at": "2026-05-01T00:00:00+00:00",
                "issues": [["BE Missing Probe 1", "BE Missing Series"]],
                "tpbs": [["BE Missing TPB Vol. 1", "BE Missing Series"]],
            }
            row = MetadataCache(
                source="canon_index", key="v1",
                payload=json.dumps(payload), fetched_at=datetime.now(UTC),
            )
            session.add(row)
            await session.commit()
    asyncio.run(_go())


def test_missing_pages_render_with_seeded_index():
    _reset()
    with _client() as client:
        _seed_index()
        for path, marker in [
            ("/missing/issues", "MISSING ISSUES"),
            ("/missing/tpbs", "MISSING TRADE PAPERBACKS"),
        ]:
            r = client.get(path)
            assert r.status_code == 200
            assert marker in r.text
            # The seeded, un-owned entry shows up as missing.
            assert "BE Missing" in r.text
    _reset()


def test_missing_refresh_status_endpoint_renders():
    _reset()
    with _client() as client:
        r = client.get("/missing/refresh/status")
        assert r.status_code == 200
        assert 'id="missing-crawl"' in r.text
    _reset()


# ---------------------------------------------------------------------------
# End-to-end crawl against a mocked category tree
# ---------------------------------------------------------------------------


def _category_route(request: httpx.Request) -> httpx.Response:
    qs = parse_qs(urlparse(str(request.url)).query)
    if qs.get("list", [None])[0] != "categorymembers":
        return httpx.Response(404)
    title = qs.get("cmtitle", [""])[0]
    cmtype = qs.get("cmtype", [""])[0]
    tree = {
        ("Category:Canon comic book issues", "subcat"): ["Category:BE Probe issues"],
        ("Category:Canon comic book issues", "page"): [],
        ("Category:BE Probe issues", "subcat"): [],
        ("Category:BE Probe issues", "page"): ["BE Probe 1", "BE Probe 2"],
        ("Category:Canon trade paperbacks", "subcat"): [],
        ("Category:Canon trade paperbacks", "page"): ["BE Probe Vol. 1"],
    }
    names = tree.get((title, cmtype), [])
    return httpx.Response(200, json={
        "query": {"categorymembers": [{"title": n} for n in names]},
    })


@respx.mock
def test_crawl_builds_and_caches_the_canon_index():
    respx.get("https://starwars.fandom.com/api.php").mock(side_effect=_category_route)
    _reset()
    with _client():
        async def _run() -> None:
            started = await ci.start_crawl()
            assert started is True
            await ci._task

        asyncio.run(_run())
        index = asyncio.run(ci.get_canon_index())

    assert index is not None
    issue_titles = {t for t, _s in index["issues"]}
    assert {"BE Probe 1", "BE Probe 2"} <= issue_titles
    tpb_titles = {t for t, _s in index["tpbs"]}
    assert "BE Probe Vol. 1" in tpb_titles
    assert ci.get_progress().phase == "Finished"
    _reset()
