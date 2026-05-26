"""Phase 12: full export -> import roundtrip preserves the library."""

from __future__ import annotations

import io
import json

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def _seed(client: TestClient) -> int:
    """Save one comic via the normal /add flow, then attach a tag and a
    second copy. Returns the comic id."""
    r = client.post(
        "/add/save",
        data={
            "title": "Roundtrip Test #1",
            "issue_number": "1",
            "publisher": "Test Publisher",
            "series": "Roundtrip Series",
            "isbn_13": "9780000000017",
            "price_paid_eur": "4.99",
        },
    )
    assert r.status_code == 200

    comics = client.get("/api/comics", params={"limit": 500}).json()
    cid = next(c["id"] for c in comics if c["title"] == "Roundtrip Test #1")

    # second copy
    r = client.post(
        f"/comic/{cid}/copies",
        data={"condition": "VF", "storage_location": "Shelf A"},
    )
    assert r.status_code == 200

    # tag
    r = client.post(f"/comic/{cid}/tags", data={"name": "roundtrip"})
    assert r.status_code == 200
    return cid


def test_export_returns_versioned_payload_with_seeded_data():
    with _client() as client:
        cid = _seed(client)

        r = client.get("/api/export")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert "longbox-backup-" in r.headers["content-disposition"]

        payload = r.json()
        # v2: dropped wishlist/pull_list. v3: moved fandom Series→Comic.
        # v4: added ComicSeries + ComicContainment link tables so a
        # backup round-trips multi-series memberships and containment.
        assert payload["version"] == 4
        assert "exported_at" in payload
        # Link tables must be present in the payload now — empty list is
        # acceptable, missing key is the bug v4 is fixing.
        assert "comic_series" in payload
        assert "comic_containment" in payload

        titles = [c["title"] for c in payload["comics"]]
        assert "Roundtrip Test #1" in titles
        # Two copies: the auto one from /add/save + the explicit one above.
        assert sum(1 for cp in payload["copies"] if cp["comic_id"] == cid) == 2
        assert any(t["name"] == "roundtrip" for t in payload["tags"])
        assert any(p["name"] == "Test Publisher" for p in payload["publishers"])


def test_import_round_trip_replaces_library():
    with _client() as client:
        cid = _seed(client)
        before = client.get("/api/export").json()

        # Sanity: collection is non-empty.
        assert len(before["comics"]) >= 1

        # Add a second, *different* comic that the import should wipe.
        r = client.post(
            "/add/save",
            data={
                "title": "Will Be Wiped",
                "issue_number": "99",
                "publisher": "Wipeable",
                "series": "Wipeable",
                "isbn_13": "9780000099999",
            },
        )
        assert r.status_code == 200
        intermediate = client.get("/api/comics", params={"limit": 500}).json()
        assert any(c["title"] == "Will Be Wiped" for c in intermediate)

        # Re-import the earlier export → wiped comic should disappear.
        buf = io.BytesIO(json.dumps(before).encode("utf-8"))
        r = client.post(
            "/admin/import",
            files={"backup": ("backup.json", buf, "application/json")},
        )
        assert r.status_code == 200
        assert "Imported" in r.text

        after = client.get("/api/comics", params={"limit": 500}).json()
        titles = [c["title"] for c in after]
        assert "Roundtrip Test #1" in titles
        assert "Will Be Wiped" not in titles


def test_round_trip_preserves_multi_series_and_containment():
    """A comic that's a multi-series member AND a containment parent
    must come back with both relationships intact after
    export → wipe → import. Pre-v4 backups dropped these silently
    because `_ENTITIES_IN_ORDER` didn't include the link tables."""
    import asyncio
    from sqlmodel import select
    from app.db import SessionLocal
    from app.models import Comic, ComicSeries, ComicContainment, Series

    with _client() as client:
        # Seed: two comics. `parent` (TPB) collects `child` (single).
        # `child` belongs to TWO series via multi-series link.
        r = client.post(
            "/add/save",
            data={
                "title": "RT Parent TPB", "publisher": "RT Pub",
                "series": "RT Series A", "isbn_13": "9780000111001",
            },
        )
        assert r.status_code == 200
        r = client.post(
            "/add/save",
            data={
                "title": "RT Child #1", "issue_number": "1",
                "publisher": "RT Pub", "series": "RT Series A",
                "isbn_13": "9780000111002",
            },
        )
        assert r.status_code == 200
        comics = client.get("/api/comics", params={"limit": 500}).json()
        parent_id = next(c["id"] for c in comics if c["title"] == "RT Parent TPB")
        child_id = next(c["id"] for c in comics if c["title"] == "RT Child #1")

        # Attach child to a SECOND series via the link table directly,
        # and add a containment edge from parent to child.
        async def _seed_links():
            async with SessionLocal() as s:
                series_b = Series(name="RT Series B")
                s.add(series_b)
                await s.flush()
                s.add(ComicSeries(
                    comic_id=child_id, series_id=series_b.id, is_primary=False,
                ))
                s.add(ComicContainment(
                    parent_id=parent_id, child_id=child_id, position=1,
                ))
                await s.commit()
                return series_b.id
        series_b_id = asyncio.run(_seed_links())

        # Export → wipe → re-import.
        before = client.get("/api/export").json()
        # Sanity: both link tables non-empty in the payload.
        assert any(
            link["comic_id"] == child_id and link["series_id"] == series_b_id
            for link in before["comic_series"]
        ), "ComicSeries link missing from export payload"
        assert any(
            link["parent_id"] == parent_id and link["child_id"] == child_id
            for link in before["comic_containment"]
        ), "ComicContainment link missing from export payload"

        # Round-trip via /admin/import (wipes + replays).
        buf = io.BytesIO(json.dumps(before).encode("utf-8"))
        r = client.post(
            "/admin/import",
            files={"backup": ("backup.json", buf, "application/json")},
        )
        assert r.status_code == 200

        # After: child must STILL be linked to two series AND parent
        # must STILL contain child.
        async def _check_after():
            async with SessionLocal() as s:
                series_links = (await s.exec(
                    select(ComicSeries.series_id)
                    .where(ComicSeries.comic_id == child_id)
                )).all()
                series_ids = {
                    r if isinstance(r, int) else r[0] for r in series_links
                }
                cont_links = (await s.exec(
                    select(ComicContainment)
                    .where(ComicContainment.parent_id == parent_id)
                    .where(ComicContainment.child_id == child_id)
                )).all()
                return series_ids, len(cont_links)
        series_ids_after, cont_count = asyncio.run(_check_after())
        # Two series links (primary + the manual B link) survived.
        assert len(series_ids_after) >= 2, (
            f"expected >=2 series links after restore, got {series_ids_after}"
        )
        assert cont_count == 1, "containment edge lost on restore"


def test_import_rejects_unknown_version():
    with _client() as client:
        buf = io.BytesIO(b'{"version": 999, "comics": []}')
        r = client.post(
            "/admin/import",
            files={"backup": ("bad.json", buf, "application/json")},
        )
        assert r.status_code == 400
        assert "version" in r.text.lower()


def test_import_rejects_invalid_json():
    with _client() as client:
        buf = io.BytesIO(b"not even json")
        r = client.post(
            "/admin/import",
            files={"backup": ("bad.json", buf, "application/json")},
        )
        assert r.status_code == 400
