"""Multi-series membership + "covered by" coverage.

A Comic can belong to N series simultaneously via the ComicSeries
link table. The Comic.series_id FK is retained as the "primary"
series for backward-compat. The series detail page queries via the
link table so an omnibus collecting issues from multiple series
shows up in every one of them.

"Covered by" inverts ComicContainment: on a TPB's detail page, show
which owned omnibuses (parents) reference it as a contained child.
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, ComicSeries, Series


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "MS Pub", "series": "MS Series"}
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


def test_save_creates_a_comicseries_link_for_the_primary_series():
    """The save path should mirror Comic.series_id into the link
    table so multi-series-aware queries pick it up. Without this,
    legacy /series/{id} (now joining via the link table) would
    return nothing for newly saved comics."""
    with _client() as client:
        cid = _save(client, title="MS Probe",
                    isbn_13="9789000020001", series="MS-PrimSer")
        comic = _comic(cid)

        async def _link_exists():
            async with SessionLocal() as session:
                return (await session.exec(
                    select(ComicSeries).where(
                        ComicSeries.comic_id == cid,
                        ComicSeries.series_id == comic.series_id,
                    )
                )).first()
        link = asyncio.run(_link_exists())
        assert link is not None
        assert link.is_primary is True


def test_comic_can_be_attached_to_a_second_series():
    """The /comic/{id}/series POST endpoint adds a non-primary link."""
    with _client() as client:
        cid = _save(client, title="Attach Probe",
                    isbn_13="9789000020101", series="AttachSer-A")
        # Find or create a second series.
        cid2 = _save(client, title="Other",
                     isbn_13="9789000020102", series="AttachSer-B")
        comic2 = _comic(cid2)
        other_series_id = comic2.series_id

        r = client.post(
            f"/comic/{cid}/series",
            data={"series_id": str(other_series_id)},
        )
        assert r.status_code == 200

        async def _links():
            async with SessionLocal() as session:
                return (await session.exec(
                    select(ComicSeries).where(ComicSeries.comic_id == cid)
                )).all()
        links = asyncio.run(_links())
        # Primary + the newly-added one.
        assert len(links) == 2
        non_primary = [l for l in links if not l.is_primary]
        assert len(non_primary) == 1
        assert non_primary[0].series_id == other_series_id


def test_comic_can_be_attached_to_a_brand_new_series_by_name():
    """`new_series_name` creates the Series row first, then links."""
    with _client() as client:
        cid = _save(client, title="New Series Probe",
                    isbn_13="9789000020201", series="NewSerSer")

        r = client.post(
            f"/comic/{cid}/series",
            data={"new_series_name": "Knights of the Old Republic: War"},
        )
        assert r.status_code == 200

        async def _check():
            async with SessionLocal() as session:
                ser = (await session.exec(
                    select(Series).where(
                        Series.name == "Knights of the Old Republic: War"
                    )
                )).first()
                assert ser is not None
                link = (await session.exec(
                    select(ComicSeries).where(
                        ComicSeries.comic_id == cid,
                        ComicSeries.series_id == ser.id,
                    )
                )).first()
                return ser, link
        ser, link = asyncio.run(_check())
        assert link is not None
        assert link.is_primary is False


def test_remove_non_primary_link_drops_it():
    with _client() as client:
        cid = _save(client, title="Rem MS",
                    isbn_13="9789000020301", series="RemMSSer")
        client.post(
            f"/comic/{cid}/series",
            data={"new_series_name": "Extra Series Foo"},
        )
        async def _get_extra():
            async with SessionLocal() as session:
                return (await session.exec(
                    select(Series).where(Series.name == "Extra Series Foo")
                )).first()
        extra = asyncio.run(_get_extra())
        assert extra is not None

        r = client.post(
            f"/comic/{cid}/series/{extra.id}/delete",
        )
        assert r.status_code == 200

        async def _gone():
            async with SessionLocal() as session:
                return (await session.exec(
                    select(ComicSeries).where(
                        ComicSeries.comic_id == cid,
                        ComicSeries.series_id == extra.id,
                    )
                )).first()
        assert asyncio.run(_gone()) is None


def test_cannot_remove_the_primary_series_link_via_this_endpoint():
    """The primary FK is sacred from this UI — use the merge UI to
    change a comic's primary series."""
    with _client() as client:
        cid = _save(client, title="Prim Guard",
                    isbn_13="9789000020401", series="PrimGuardSer")
        comic = _comic(cid)
        r = client.post(
            f"/comic/{cid}/series/{comic.series_id}/delete",
        )
        assert r.status_code == 422


def test_series_detail_lists_comics_attached_via_link_table():
    """An omnibus whose primary series is "KotOR" but which is ALSO
    attached to "KotOR: War" via the multi-series form should appear
    on BOTH series detail pages."""
    with _client() as client:
        omni = _save(client, title="MS Omnibus",
                     isbn_13="9789000020501", series="KotOR Primary MS")
        # Create a 2nd series and attach the omnibus to it.
        _save(client, title="War issue 1", isbn_13="9789000020502",
              series="KotOR War MS")

        async def _war_series_id():
            async with SessionLocal() as session:
                ser = (await session.exec(
                    select(Series).where(Series.name == "KotOR War MS")
                )).first()
                return ser.id
        war_id = asyncio.run(_war_series_id())
        client.post(
            f"/comic/{omni}/series",
            data={"series_id": str(war_id)},
        )

        # Hit each series page; both should list the omnibus.
        async def _primary_series_id():
            async with SessionLocal() as session:
                ser = (await session.exec(
                    select(Series).where(Series.name == "KotOR Primary MS")
                )).first()
                return ser.id
        primary_id = asyncio.run(_primary_series_id())

        r1 = client.get(f"/series/{primary_id}")
        r2 = client.get(f"/series/{war_id}")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert f'href="/comic/{omni}"' in r1.text
        assert f'href="/comic/{omni}"' in r2.text


def test_covered_by_section_lists_parent_omnibuses_on_child_detail():
    """The CONTAINS relationship has an inverse: when viewing the
    contained TPB, the comic detail page shows which owned omnibus
    references it as a child."""
    with _client() as client:
        omni = _save(client, title="CB Omnibus",
                     isbn_13="9789000020601", series="CBOmniSer")
        tpb = _save(client, title="CB TPB",
                    isbn_13="9789000020602", series="CBTPBSer")
        client.post(f"/comic/{omni}/contains", data={"child_id": str(tpb)})

        r = client.get(f"/comic/{tpb}")
        assert r.status_code == 200
        assert "COVERED BY" in r.text
        assert f'href="/comic/{omni}"' in r.text


def test_comic_detail_shows_series_management_widget():
    """The new SERIES section must be present on every comic detail
    page so the user can attach additional series memberships."""
    with _client() as client:
        cid = _save(client, title="MS Widget",
                    isbn_13="9789000020701", series="MSWidgetSer")
        r = client.get(f"/comic/{cid}")
        assert r.status_code == 200
        assert 'id="comic-series-section"' in r.text
        assert "Add to another series" in r.text


def test_comic_series_search_excludes_already_linked_series():
    """The typeahead shouldn't suggest a series the comic is already
    in — primary FK or link table."""
    with _client() as client:
        cid = _save(client, title="Excl Probe",
                    isbn_13="9789000020801", series="ExclSer-Primary")

        r = client.get(
            f"/comic/{cid}/series/search",
            params={"q": "ExclSer-Primary"},
        )
        assert r.status_code == 200
        # The primary series shouldn't appear as a match — only the
        # "Create new" option remains.
        assert "Create new" in r.text
        # No `series_id` hidden value pointing at the primary series.
        comic = _comic(cid)
        assert f'value="{comic.series_id}"' not in r.text
