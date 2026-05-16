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


# ─────────────────────  Auto-inference from collected_issues  ───────────── #


def test_derive_series_names_extracts_unique_series_from_issue_list():
    """Unit-test the parser directly. A typical omnibus collected-
    issues blob mixes multiple underlying singles series, plus one-
    shots and prose. We want the distinct singles-series names back,
    de-duped case-insensitively, in first-seen order."""
    from app.services.collected_issues import derive_series_names

    raw = (
        "Knights of the Old Republic 0\n"
        "The Taris Holofeed: Prime Edition\n"  # one-shot — no trailing num
        "Knights of the Old Republic 1\n"
        "Knights of the Old Republic 2\n"
        "Knights of the Old Republic: War 1\n"
        "Knights of the Old Republic: War 2\n"
        "Republic 78\n"
        "Republic 79\n"
        "Purge (comic book)\n"                  # no trailing num
        "KNIGHTS OF THE OLD REPUBLIC 3\n"        # case-insensitive dupe
    )
    assert derive_series_names(raw) == [
        "Knights of the Old Republic",
        "Knights of the Old Republic: War",
        "Republic",
    ]


def test_derive_series_names_handles_letter_suffix_issue_numbers():
    """Marvel-style "12A" / "0B" variant issue numbers should still
    parse to the series name."""
    from app.services.collected_issues import derive_series_names

    raw = "Star Wars 12A\nStar Wars 12B\nStar Wars 13\n"
    assert derive_series_names(raw) == ["Star Wars"]


def test_derive_series_names_returns_empty_for_singles_and_prose():
    """Singles comics (empty collected_issues) and free-form prose
    ("COLLECTING: A 1-5, B 1") shouldn't produce any inferred
    series."""
    from app.services.collected_issues import derive_series_names

    assert derive_series_names(None) == []
    assert derive_series_names("") == []
    assert derive_series_names("COLLECTING: Star Wars 1-50, Vader 1") == []


def test_save_auto_attaches_inferred_series_for_omnibus_like_comic():
    """The full save → inference flow: when /add/save lands a comic
    whose collected_issues blob references multiple underlying
    series, the inference background task should attach the comic to
    each of them — without the user doing anything.

    Mocks Wookieepedia's get_article to return canonical names; the
    inferrer skips groups whose canonical resolution fails, so the
    mock has to cover every sample issue title that appears in
    `collected_issues`."""
    from unittest.mock import patch
    from app.routers.add import _attach_inferred_series
    from app.models import ComicSeries
    from app.services.schemas import LookupCandidate

    async def fake_get_article(title):
        if title.startswith("Knights of the Old Republic INF"):
            return LookupCandidate(
                source="wookieepedia", source_id=title, title=title,
                series="Knights of the Old Republic INF",
            )
        if title.startswith("Knights of the Old Republic: War INF"):
            return LookupCandidate(
                source="wookieepedia", source_id=title, title=title,
                series="Knights of the Old Republic: War INF",
            )
        return None

    async def fake_get_series_issues(article):
        return [f"{article} stub-issue"]  # non-empty so the inferrer creates the row
    with patch("app.services.wookieepedia.get_article", side_effect=fake_get_article), \
         patch("app.services.wookieepedia.get_series_issues", side_effect=fake_get_series_issues), \
         _client() as client:
        cid = _save(client, title="Inf Omnibus",
                    isbn_13="9789000030001",
                    series="Inf Primary Series",
                    publisher="Inf Pub")

        async def _seed():
            async with SessionLocal() as session:
                c = await session.get(Comic, cid)
                c.collected_issues = (
                    "Knights of the Old Republic INF 1\n"
                    "Knights of the Old Republic INF 2\n"
                    "Knights of the Old Republic: War INF 1\n"
                )
                session.add(c)
                await session.commit()
        asyncio.run(_seed())

        asyncio.run(_attach_inferred_series(cid))

        async def _linked_names():
            async with SessionLocal() as session:
                rows = (await session.exec(
                    select(Series.name)
                    .join(ComicSeries, ComicSeries.series_id == Series.id)
                    .where(ComicSeries.comic_id == cid)
                )).all()
                return {r if isinstance(r, str) else r[0] for r in rows}
        assert "Inf Primary Series" in asyncio.run(_linked_names())
        names = asyncio.run(_linked_names())
        assert "Knights of the Old Republic INF" in names
        assert "Knights of the Old Republic: War INF" in names


def test_inference_is_idempotent():
    """Running the inferrer twice on the same comic shouldn't create
    duplicate link rows."""
    from unittest.mock import patch
    from app.routers.add import _attach_inferred_series
    from app.models import ComicSeries
    from app.services.schemas import LookupCandidate
    from sqlalchemy import func as _func

    async def fake_get_article(title):
        if title.startswith("Idem Inferred Series"):
            return LookupCandidate(
                source="wookieepedia", source_id=title, title=title,
                series="Idem Inferred Series",
            )
        return None

    async def fake_get_series_issues(article):
        return [f"{article} stub-issue"]
    with patch("app.services.wookieepedia.get_article", side_effect=fake_get_article), \
         patch("app.services.wookieepedia.get_series_issues", side_effect=fake_get_series_issues), \
         _client() as client:
        cid = _save(client, title="Idem Inf",
                    isbn_13="9789000030101",
                    series="Idem Inf Primary")
        async def _seed():
            async with SessionLocal() as session:
                c = await session.get(Comic, cid)
                c.collected_issues = "Idem Inferred Series 1\nIdem Inferred Series 2\n"
                session.add(c)
                await session.commit()
        asyncio.run(_seed())

        asyncio.run(_attach_inferred_series(cid))
        asyncio.run(_attach_inferred_series(cid))

        async def _count():
            async with SessionLocal() as session:
                return (await session.exec(
                    select(_func.count())
                    .select_from(ComicSeries)
                    .where(ComicSeries.comic_id == cid)
                )).first()
        n = asyncio.run(_count())
        n = n[0] if isinstance(n, tuple) else n
        # Primary + exactly one inferred — no duplicates.
        assert int(n) == 2


def test_inferred_series_resolves_to_canonical_article_title():
    """The inferrer should look up the SAMPLE issue's article on
    Wookieepedia and read its `series=` infobox to get the proper
    article title — NOT just blindly trust the trailing-number-
    stripped guess. The guess "Knights of the Old Republic" maps to
    "Star Wars: Knights of the Old Republic (comic series)" on the
    wiki, and that's what should end up in Series.name AND
    Series.source_id (so auto-link can fetch the issue list)."""
    from unittest.mock import patch
    from app.routers.add import _attach_inferred_series
    from app.services.schemas import LookupCandidate

    async def fake_get_article(title):
        # The inferrer should call us with a sample issue title — NOT
        # the trailing-number-stripped guess. Verify and return what
        # the real Wookieepedia infobox would yield.
        if title == "Canonical Test Series CTS 1":
            return LookupCandidate(
                source="wookieepedia",
                source_id=title,
                title=title,
                # `_pick_specific_series` resolves the level-1 wikilink
                # in this issue article's `series=` infobox to a more
                # specific name with the `(comic series)` disambiguator.
                series="Star Wars: Canonical Test Series CTS (comic series)",
            )
        return None

    async def fake_get_series_issues(article):
        return [f"{article} stub-issue"]
    with patch("app.services.wookieepedia.get_article", side_effect=fake_get_article), \
         patch("app.services.wookieepedia.get_series_issues", side_effect=fake_get_series_issues), \
         _client() as client:
        cid = _save(client, title="Canonical Probe",
                    isbn_13="9789000030301",
                    series="CTS Primary")
        async def _seed():
            async with SessionLocal() as session:
                c = await session.get(Comic, cid)
                c.collected_issues = (
                    "Canonical Test Series CTS 1\n"
                    "Canonical Test Series CTS 2\n"
                    "Canonical Test Series CTS 3\n"
                )
                session.add(c)
                await session.commit()
        asyncio.run(_seed())

        asyncio.run(_attach_inferred_series(cid))

        async def _check():
            async with SessionLocal() as session:
                # The canonical title (not the guess) should be in the DB.
                canonical = (await session.exec(
                    select(Series).where(
                        Series.name == "Star Wars: Canonical Test Series CTS (comic series)"
                    )
                )).first()
                guess = (await session.exec(
                    select(Series).where(Series.name == "Canonical Test Series CTS")
                )).first()
                return canonical, guess
        canonical, guess = asyncio.run(_check())
        # Canonical row exists and is properly stamped for auto-link.
        assert canonical is not None
        assert canonical.source == "wookieepedia"
        # source_id is the SERIES ARTICLE TITLE (cand.series), not the
        # sample issue title we used to look it up. That's what
        # /series/{id}/auto-link expects to feed to get_series_issues.
        assert canonical.source_id == "Star Wars: Canonical Test Series CTS (comic series)"
        # The trailing-number-stripped guess should NOT exist as a
        # separate row — we used the canonical from upstream.
        assert guess is None


def test_inferred_series_also_pre_populates_expected_issues():
    """When the inferrer creates a Series, it should ALSO call
    `get_series_issues` against the canonical article and write the
    result to `expected_issues`. Otherwise the /series/{id} page
    shows no missing-issues progress until the user clicks the
    auto-link button per inferred series — and that's exactly what
    the user complained about on /series/2 and /series/3."""
    from unittest.mock import patch
    from app.routers.add import _attach_inferred_series
    from app.services.schemas import LookupCandidate

    async def fake_get_article(title):
        if title == "Pre Pop Series PPS 1":
            return LookupCandidate(
                source="wookieepedia", source_id=title, title=title,
                series="Star Wars: Pre Pop Series PPS (comic series)",
            )
        return None

    fake_issues = [
        "Pre Pop Series PPS 1",
        "Pre Pop Series PPS 2",
        "Pre Pop Series PPS 3",
    ]

    async def fake_get_series_issues(article):
        if article == "Star Wars: Pre Pop Series PPS (comic series)":
            return fake_issues
        return []

    with patch("app.services.wookieepedia.get_article", side_effect=fake_get_article), \
         patch("app.services.wookieepedia.get_series_issues", side_effect=fake_get_series_issues), \
         _client() as client:
        cid = _save(client, title="Pre Pop Probe",
                    isbn_13="9789000040001",
                    series="Pre Pop Primary")
        async def _seed():
            async with SessionLocal() as session:
                c = await session.get(Comic, cid)
                c.collected_issues = "Pre Pop Series PPS 1\nPre Pop Series PPS 2\n"
                session.add(c)
                await session.commit()
        asyncio.run(_seed())

        asyncio.run(_attach_inferred_series(cid))

        async def _check():
            async with SessionLocal() as session:
                return (await session.exec(
                    select(Series).where(
                        Series.name == "Star Wars: Pre Pop Series PPS (comic series)"
                    )
                )).first()
        ser = asyncio.run(_check())
        assert ser is not None
        assert ser.expected_issues is not None
        # All three fake issues should be there, newline-joined.
        for issue in fake_issues:
            assert issue in ser.expected_issues


def test_inferred_series_skips_when_canonical_resolution_fails():
    """When the issue article can't be resolved on Wookieepedia (404
    or network error), the inferrer must SKIP rather than create a
    guess-named Series row. Earlier behaviour was to write the guess
    name, but that produces orphan rows that are awkward to merge
    later — and worse, it can happen mid-batch when one HTTP call
    blips, leaving the comic in a half-correct state. The cold-
    start backfill / explicit refresh will re-try later when the
    network is happy."""
    from unittest.mock import patch
    from app.routers.add import _attach_inferred_series

    async def fake_get_article(title):
        return None  # simulate "not found"

    with patch("app.services.wookieepedia.get_article", side_effect=fake_get_article), \
         _client() as client:
        cid = _save(client, title="Skip Probe",
                    isbn_13="9789000030401",
                    series="Fb Primary")
        async def _seed():
            async with SessionLocal() as session:
                c = await session.get(Comic, cid)
                c.collected_issues = "FB Mystery Series FBMS 1\nFB Mystery Series FBMS 2\n"
                session.add(c)
                await session.commit()
        asyncio.run(_seed())

        asyncio.run(_attach_inferred_series(cid))

        async def _check():
            async with SessionLocal() as session:
                guess = (await session.exec(
                    select(Series).where(Series.name == "FB Mystery Series FBMS")
                )).first()
                return guess
        # No row created. The cold-start backfill or a manual refresh
        # will retry resolution later.
        assert asyncio.run(_check()) is None


def test_inferred_series_inherits_primary_publisher():
    """Newly-created Series rows from inference should pick up the
    publisher of the comic's primary series, so the library publisher
    facet doesn't sprout a stray '(unset)' chip per inferred series."""
    from unittest.mock import patch
    from app.routers.add import _attach_inferred_series
    from app.services.schemas import LookupCandidate

    async def fake_get_article(title):
        if title.startswith("Pub Inferred Series"):
            return LookupCandidate(
                source="wookieepedia", source_id=title, title=title,
                series="Pub Inferred Series",
            )
        return None

    async def fake_get_series_issues(article):
        return [f"{article} stub-issue"]
    with patch("app.services.wookieepedia.get_article", side_effect=fake_get_article), \
         patch("app.services.wookieepedia.get_series_issues", side_effect=fake_get_series_issues), \
         _client() as client:
        cid = _save(client, title="Pub Inh",
                    isbn_13="9789000030201",
                    series="Pub Inh Primary",
                    publisher="Pub Inh Marvel")
        async def _seed():
            async with SessionLocal() as session:
                c = await session.get(Comic, cid)
                c.collected_issues = "Pub Inferred Series 1\n"
                session.add(c)
                await session.commit()
        asyncio.run(_seed())

        asyncio.run(_attach_inferred_series(cid))

        async def _check():
            async with SessionLocal() as session:
                ser = (await session.exec(
                    select(Series).where(Series.name == "Pub Inferred Series")
                )).first()
                assert ser is not None
                assert ser.publisher_id is not None
                return ser.publisher_id
        pub_id = asyncio.run(_check())
        # Confirm publisher name matches what the comic's primary
        # series carries.
        async def _pub():
            from app.models import Publisher
            async with SessionLocal() as session:
                return await session.get(Publisher, pub_id)
        pub = asyncio.run(_pub())
        assert pub.name == "Pub Inh Marvel"


def test_inference_renames_guess_row_to_canonical_when_canonical_doesnt_exist():
    """Regression: on /comic/1 the user saw two series rows
    side-by-side, "Knights of the Old Republic: War" (the old
    guess-only inference) AND "Star Wars: Knights of the Old
    Republic: War" (the new canonical). The inferrer must collapse
    the dupes by renaming the guess row into the canonical."""
    from unittest.mock import patch
    from app.routers.add import _attach_inferred_series
    from app.services.schemas import LookupCandidate

    async def fake_get_article(title):
        if title == "Guess Vs Canonical GVC 1":
            return LookupCandidate(
                source="wookieepedia", source_id=title, title=title,
                series="Star Wars: Guess Vs Canonical GVC (comic series)",
            )
        return None

    with patch("app.services.wookieepedia.get_article", side_effect=fake_get_article), \
         _client() as client:
        cid = _save(client, title="GvC Probe",
                    isbn_13="9789000030501", series="GvC Primary")

        async def _seed_legacy():
            async with SessionLocal() as session:
                # Simulate the legacy state: a Series row exists with
                # the guess name (created before canonical resolution
                # landed), and the comic is already linked to it.
                from app.models import ComicSeries
                legacy = Series(name="Guess Vs Canonical GVC")
                session.add(legacy)
                await session.flush()
                session.add(ComicSeries(
                    comic_id=cid, series_id=legacy.id, is_primary=False,
                ))
                # Stamp collected_issues so the inferrer runs.
                c = await session.get(Comic, cid)
                c.collected_issues = (
                    "Guess Vs Canonical GVC 1\n"
                    "Guess Vs Canonical GVC 2\n"
                )
                session.add(c)
                await session.commit()
                return legacy.id
        legacy_id = asyncio.run(_seed_legacy())

        asyncio.run(_attach_inferred_series(cid))

        async def _check():
            async with SessionLocal() as session:
                renamed = await session.get(Series, legacy_id)
                guess_still = (await session.exec(
                    select(Series).where(Series.name == "Guess Vs Canonical GVC")
                )).first()
                canon = (await session.exec(
                    select(Series).where(
                        Series.name == "Star Wars: Guess Vs Canonical GVC (comic series)"
                    )
                )).first()
                return renamed, guess_still, canon
        renamed, guess_still, canon = asyncio.run(_check())
        # Same row, new name — id preserved so existing links still work.
        assert renamed.id == legacy_id
        assert renamed.name == "Star Wars: Guess Vs Canonical GVC (comic series)"
        assert renamed.source == "wookieepedia"
        # Guess name no longer exists; canonical points at our row.
        assert guess_still is None
        assert canon is not None
        assert canon.id == legacy_id


def test_inference_merges_when_both_guess_and_canonical_rows_exist():
    """The harder case: BOTH a guess-named row AND a canonical-named
    row already exist (the user toggled inference paths over time).
    The inferrer should collapse the guess into the canonical,
    reassigning every link and the primary FK."""
    from unittest.mock import patch
    from app.routers.add import _attach_inferred_series
    from app.services.schemas import LookupCandidate
    from app.models import ComicSeries

    async def fake_get_article(title):
        if title == "Merge Test MT 1":
            return LookupCandidate(
                source="wookieepedia", source_id=title, title=title,
                series="Star Wars: Merge Test MT (comic series)",
            )
        return None

    with patch("app.services.wookieepedia.get_article", side_effect=fake_get_article), \
         _client() as client:
        cid = _save(client, title="Merge Probe",
                    isbn_13="9789000030601", series="Merge Primary")

        async def _seed():
            async with SessionLocal() as session:
                guess = Series(name="Merge Test MT")
                canon = Series(name="Star Wars: Merge Test MT (comic series)")
                session.add(guess)
                session.add(canon)
                await session.flush()
                # Link the comic to the guess row only.
                session.add(ComicSeries(
                    comic_id=cid, series_id=guess.id, is_primary=False,
                ))
                c = await session.get(Comic, cid)
                c.collected_issues = "Merge Test MT 1\n"
                session.add(c)
                await session.commit()
                return guess.id, canon.id
        guess_id, canon_id = asyncio.run(_seed())

        asyncio.run(_attach_inferred_series(cid))

        async def _check():
            async with SessionLocal() as session:
                # Guess row is gone.
                gone = await session.get(Series, guess_id)
                # Canonical row still exists.
                canon = await session.get(Series, canon_id)
                # Link reassigned to canonical, no link to the (gone) guess.
                links = (await session.exec(
                    select(ComicSeries).where(ComicSeries.comic_id == cid)
                )).all()
                target_series_ids = {l.series_id for l in links}
                return gone, canon, target_series_ids
        gone, canon, target_series_ids = asyncio.run(_check())
        assert gone is None
        assert canon is not None
        assert canon_id in target_series_ids
        assert guess_id not in target_series_ids


def test_backfill_normalises_primary_flag_to_a_single_row_per_comic():
    """Regression for "comic has multiple PRIMARY badges": when the
    series-rename / merge flow reassigns Comic.series_id over time,
    older ComicSeries rows can keep is_primary=True. The backfill
    must demote any link whose series_id no longer matches the
    current Comic.series_id."""
    from app.models import ComicSeries
    from app.services.fandoms import backfill_comic_series_links

    with _client() as client:
        cid = _save(client, title="Primary Drift",
                    isbn_13="9789000030701", series="Drift Series")
        comic = _comic(cid)
        original_primary = comic.series_id

        async def _seed_drift():
            async with SessionLocal() as session:
                # Create a second series and write a STALE primary link
                # for the same comic — simulating what happens when
                # /series/{id}/auto-link reassigns Comic.series_id after
                # the original primary link was already created.
                from app.models import ComicSeries as CS
                other = Series(name="Drift Other Series")
                session.add(other)
                await session.flush()
                session.add(CS(
                    comic_id=cid, series_id=other.id, is_primary=True,
                ))
                await session.commit()
                return other.id
        other_id = asyncio.run(_seed_drift())

        # Backfill should normalise: only the link matching
        # Comic.series_id stays primary.
        asyncio.run(backfill_comic_series_links())

        async def _check():
            async with SessionLocal() as session:
                rows = (await session.exec(
                    select(ComicSeries).where(ComicSeries.comic_id == cid)
                )).all()
                return rows
        rows = asyncio.run(_check())
        primaries = [r for r in rows if r.is_primary]
        assert len(primaries) == 1
        assert primaries[0].series_id == original_primary
        # The "drifted" stale row is demoted (still present but not primary).
        other_row = [r for r in rows if r.series_id == other_id][0]
        assert other_row.is_primary is False


def test_clean_decodes_html_entities():
    """Regression: timeline fields like "3964&ndash;3962 BBY" rendered
    with the literal entity instead of the en-dash character.
    `_clean` now html.unescape()s the result."""
    from app.services.wookieepedia import _clean
    assert _clean("3964&ndash;3962 BBY") == "3964–3962 BBY"
    assert _clean("Foo &amp; Bar") == "Foo & Bar"
    assert _clean("non&mdash;breaking") == "non—breaking"


def test_comic_delete_prunes_all_linked_series_not_just_primary():
    """Regression: deleting the only comic in a multi-linked series
    set should orphan-prune EVERY one of those series, not just the
    primary. Before this fix, the comic delete cascade only checked
    Comic.series_id and left inferred non-primary series behind."""
    from app.models import ComicSeries

    with _client() as client:
        cid = _save(client, title="CD Probe",
                    isbn_13="9789000040101", series="CD Primary")
        async def _seed_extras():
            async with SessionLocal() as session:
                # Add two extra series as non-primary links.
                a = Series(name="CD Extra Alpha")
                b = Series(name="CD Extra Beta")
                session.add(a); session.add(b)
                await session.flush()
                session.add(ComicSeries(comic_id=cid, series_id=a.id, is_primary=False))
                session.add(ComicSeries(comic_id=cid, series_id=b.id, is_primary=False))
                await session.commit()
                return a.id, b.id
        a_id, b_id = asyncio.run(_seed_extras())

        # Delete the comic; all three series should orphan-prune.
        r = client.post(f"/comic/{cid}/delete")
        assert r.status_code == 204

        async def _check():
            async with SessionLocal() as session:
                ghost_primary = (await session.exec(
                    select(Series).where(Series.name == "CD Primary")
                )).first()
                ghost_a = await session.get(Series, a_id)
                ghost_b = await session.get(Series, b_id)
                return ghost_primary, ghost_a, ghost_b
        ghost_primary, ghost_a, ghost_b = asyncio.run(_check())
        assert ghost_primary is None
        assert ghost_a is None
        assert ghost_b is None


def test_series_delete_endpoint_drops_series_and_unlinks_comics():
    """The /series/{id}/delete endpoint removes the series row, sets
    Comic.series_id to NULL for any comic that had it as primary,
    and drops every ComicSeries link pointing at it. The comics
    themselves stay in the library."""
    from app.models import ComicSeries

    with _client() as client:
        cid = _save(client, title="SD Comic",
                    isbn_13="9789000040201", series="SD Primary")
        comic = _comic(cid)
        sid = comic.series_id

        # Without confirm=yes it must refuse.
        r = client.post(f"/series/{sid}/delete")
        assert r.status_code == 422

        # With confirm=yes it deletes.
        r = client.post(f"/series/{sid}/delete", data={"confirm": "yes"})
        assert r.status_code == 204

        async def _check():
            async with SessionLocal() as session:
                ghost = await session.get(Series, sid)
                comic_still = await session.get(Comic, cid)
                link_still = (await session.exec(
                    select(ComicSeries).where(ComicSeries.series_id == sid)
                )).first()
                return ghost, comic_still, link_still
        ghost, comic_still, link_still = asyncio.run(_check())
        # Series gone; comic still there (but with series_id=None);
        # link gone.
        assert ghost is None
        assert comic_still is not None
        assert comic_still.series_id is None
        assert link_still is None


def test_compute_progress_counts_comics_linked_via_link_table():
    """Regression for "home page series progress is 0 even for full
    series": `compute_progress` was only counting comics whose
    PRIMARY series_id matched. Inferred series — where the omnibus
    is linked via the multi-series link table, not the primary FK —
    showed 0/N owned because the comic was invisible to the query.

    With the fix, the omnibus's collected_issues is consulted via
    the trade-match path and the inferred series shows 100% complete
    when the omnibus covers all expected issues."""
    from app.models import ComicSeries
    from app.services.series_progress import compute_progress

    with _client() as client:
        omni = _save(client, title="Prog Omni",
                     isbn_13="9789000040401",
                     series="Prog Primary Imprint")

        # Create an inferred series row + link the omnibus to it via
        # the link table (non-primary). Mirrors what
        # `_attach_inferred_series` does.
        async def _seed():
            async with SessionLocal() as session:
                comic = await session.get(Comic, omni)
                comic.collected_issues = (
                    "Prog Inner 1\nProg Inner 2\nProg Inner 3\n"
                )
                session.add(comic)
                inner = Series(
                    name="Prog Inner Series",
                    source="wookieepedia",
                    source_id="Prog Inner Series",
                    expected_issues="Prog Inner 1\nProg Inner 2\nProg Inner 3",
                )
                session.add(inner)
                await session.flush()
                session.add(ComicSeries(
                    comic_id=omni, series_id=inner.id, is_primary=False,
                ))
                await session.commit()
                return inner.id
        inner_id = asyncio.run(_seed())

        async def _check():
            async with SessionLocal() as session:
                return await compute_progress(session, [inner_id])
        progress = asyncio.run(_check())
        assert inner_id in progress
        # All 3 inner issues covered via the omnibus's collected_issues.
        assert progress[inner_id].owned == 3
        assert progress[inner_id].total == 3
        assert progress[inner_id].is_complete


def test_series_merge_reassigns_comicseries_links_not_just_primary_fk():
    """Regression: merging series A into series B must move every
    ComicSeries link from A→B, not just the Comic.series_id FK.
    Otherwise dangling links to the deleted A row break later
    orphan-prune attempts."""
    from app.models import ComicSeries

    with _client() as client:
        # Save two comics in different series; we'll merge one into
        # the other and verify the link table got moved too.
        cid_src = _save(client, title="Merge Link Src",
                        isbn_13="9789000040301", series="Merge Link Source")
        cid_tgt = _save(client, title="Merge Link Tgt",
                        isbn_13="9789000040302", series="Merge Link Target")
        comic_src = _comic(cid_src)
        comic_tgt = _comic(cid_tgt)
        src_id = comic_src.series_id
        tgt_id = comic_tgt.series_id

        r = client.post(f"/series/{src_id}/merge", data={"target_id": tgt_id})
        assert r.status_code == 204

        async def _check():
            async with SessionLocal() as session:
                # No surviving link pointing at the deleted source.
                dangling = (await session.exec(
                    select(ComicSeries).where(ComicSeries.series_id == src_id)
                )).all()
                # The source comic's link now points at the target.
                src_links = (await session.exec(
                    select(ComicSeries).where(ComicSeries.comic_id == cid_src)
                )).all()
                return dangling, [l.series_id for l in src_links]
        dangling, src_link_targets = asyncio.run(_check())
        assert dangling == []
        assert tgt_id in src_link_targets


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
