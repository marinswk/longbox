"""Series detail page + missing-issues detection + merge tool.

GET  /series/{id}            — detail page with progress bar, owned and
                                 missing-issue lists, refresh form, and
                                 the merge form.
POST /series/{id}/refresh    — accepts source/source_id, persists them
                                 onto the Series row, fetches the upstream
                                 issue list, recomputes.
POST /series/{id}/merge      — collapse this series into another:
                                 reassign all owned `Comic.series_id`s to
                                 the target, copy missing source/issue
                                 data from source if target is empty,
                                 delete the source row.

Match logic for owned-vs-missing:

  1. Precise:  Comic.source_id  ==  expected article title
  2. Fallback: trailing digits of expected article title  ==  Comic.issue_number

The fallback covers older comics that pre-date the source linkage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from collections import Counter, defaultdict

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from datetime import UTC, datetime

from sqlalchemy import func, or_, update

from app.db import get_session
from app.models import Comic, Publisher, Series
from app.services import comicvine, metron, wookieepedia
from app.services.errors import UpstreamRateLimit
from app.services.series_progress import compute_progress, match_owned, parse_canceled, parse_expected

# Map of source-name -> async fetcher that takes a source_id (article
# title for Wookieepedia, numeric volume/series id for CV/Metron) and
# returns a list of expected-issue labels.
_FETCHERS: dict[str, callable] = {
    "wookieepedia": wookieepedia.get_series_issues,
    "comicvine":    comicvine.get_volume_issues,
    "metron":       metron.get_series_issues,
}

router = APIRouter(tags=["series"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def _load(session: AsyncSession, series_id: int) -> dict:
    series = await session.get(Series, series_id)
    if series is None:
        raise HTTPException(status_code=404, detail="series not found")
    publisher = (
        await session.get(Publisher, series.publisher_id)
        if series.publisher_id
        else None
    )

    # Multi-series-aware ownership query. A comic appears here when
    # EITHER its primary `series_id` FK matches OR it has a row in
    # ComicSeries pointing at this series. The OR lets us cover legacy
    # data (pre-multi-series) without a forced backfill while also
    # picking up newly-attached series links from the comic detail
    # page's "Add to another series" form.
    from app.models import ComicSeries
    comics_result = await session.exec(
        select(Comic)
        .join(
            ComicSeries,
            (ComicSeries.comic_id == Comic.id)
            & (ComicSeries.series_id == series_id),
            isouter=True,
        )
        .where(
            (Comic.series_id == series_id)
            | (ComicSeries.series_id == series_id)
        )
        .distinct()
        .order_by(Comic.issue_number)
    )
    comics = list(comics_result.all())

    expected = parse_expected(series)
    pairs, owned = match_owned(expected, comics)

    # Comics in this series that aren't matched against any expected entry —
    # for example one-shots, FCBD specials, etc. Display them under "Other"
    # so they don't disappear from the page. Trades that are credited via
    # collected_issues still count as "matched" — they shouldn't appear in
    # the extras list just because they collect issues.
    matched_ids: set[int] = set()
    for pair in pairs:
        if pair.direct is not None:
            matched_ids.add(pair.direct.id)
        if pair.trade is not None:
            matched_ids.add(pair.trade.id)
    extras = [c for c in comics if c.id not in matched_ids]

    progress_pct = 0
    if expected:
        progress_pct = int(round(100 * owned / len(expected)))

    # Other series the user could merge this one into. Sort by name so the
    # dropdown is human-scanable; cap at 200 so we don't render thousands.
    others_result = await session.exec(
        select(Series).where(Series.id != series_id).order_by(Series.name).limit(200)
    )
    other_series = list(others_result.all())

    # Flat owned-comics list for the cover collage at the top of the page.
    # Prefer the direct issue match; fall back to the trade that collects it.
    # `extras` (one-shots, FCBD, etc.) come last so they're still surfaced.
    seen_ids: set[int] = set()
    owned_comics: list[Comic] = []
    for pair in pairs:
        c = pair.direct or pair.trade
        if c is not None and c.id not in seen_ids:
            owned_comics.append(c)
            seen_ids.add(c.id)
    for c in extras:
        if c.id not in seen_ids:
            owned_comics.append(c)
            seen_ids.add(c.id)

    canceled = parse_canceled(series)
    return {
        "series": series,
        "publisher": publisher,
        "expected_pairs": pairs,
        "expected_total": len(expected),
        "owned_count": owned,
        "missing_count": max(0, len(expected) - owned),
        "canceled_titles": canceled,
        "extras": extras,
        "owned_comics": owned_comics,
        "progress_pct": progress_pct,
        "other_series": other_series,
    }


@router.get("/series", response_class=HTMLResponse)
async def series_index(
    request: Request,
    session: SessionDep,
    publisher: list[str] = Query(default=[]),
    fandom: list[str] = Query(default=[]),
    status: list[str] = Query(default=[]),
    q: str = Query(default=""),
    sort: str = Query(default="name_asc"),
    group: str = Query(default="none"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
) -> HTMLResponse:
    if sort not in _SORT_VALUES:
        sort = "name_asc"
    if group not in _GROUP_VALUES:
        group = "none"
    publisher = _drop_empty(publisher)
    fandom = _drop_empty(fandom)
    status = [s for s in _drop_empty(status) if s in _STATUS_VALUES]
    ctx = await _build_series_index(
        session,
        publishers=publisher, fandoms=fandom, statuses=status,
        q=q, sort=sort, page=page, page_size=page_size, group=group,
    )
    return templates.TemplateResponse(request, "series_index.html", ctx)


@router.get("/series/grid", response_class=HTMLResponse)
async def series_index_grid(
    request: Request,
    session: SessionDep,
    publisher: list[str] = Query(default=[]),
    fandom: list[str] = Query(default=[]),
    status: list[str] = Query(default=[]),
    q: str = Query(default=""),
    sort: str = Query(default="name_asc"),
    group: str = Query(default="none"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
) -> HTMLResponse:
    if sort not in _SORT_VALUES:
        sort = "name_asc"
    if group not in _GROUP_VALUES:
        group = "none"
    publisher = _drop_empty(publisher)
    fandom = _drop_empty(fandom)
    status = [s for s in _drop_empty(status) if s in _STATUS_VALUES]
    ctx = await _build_series_index(
        session,
        publishers=publisher, fandoms=fandom, statuses=status,
        q=q, sort=sort, page=page, page_size=page_size, group=group,
    )
    return templates.TemplateResponse(
        request, "partials/_series_grid.html", ctx,
    )


@router.get("/series/{series_id}", response_class=HTMLResponse)
async def series_detail(
    series_id: int, request: Request, session: SessionDep
) -> HTMLResponse:
    ctx = await _load(session, series_id)
    return templates.TemplateResponse(request, "series_detail.html", ctx)


@router.post("/series/{series_id}/refresh")
async def series_refresh(
    series_id: int,
    session: SessionDep,
    source: str = Form(""),
    source_id: str = Form(""),
) -> Response:
    """Pull the upstream issue list and store it on the Series row.

    Submitted source/source_id override what's already on the row, so the
    user can adopt or change the source link without leaving the page.

    Sources supported: Wookieepedia (article title), ComicVine (numeric
    volume id, e.g. `42537`), Metron (numeric series id).
    """
    series = await session.get(Series, series_id)
    if series is None:
        raise HTTPException(status_code=404, detail="series not found")

    src = (source or series.source or "").strip()
    sid = (source_id or series.source_id or "").strip()
    if not src or not sid:
        raise HTTPException(status_code=400, detail="source and source_id required")

    fetcher = _FETCHERS.get(src)
    if fetcher is None:
        raise HTTPException(
            status_code=400,
            detail=f"unsupported source {src!r}; expected one of {sorted(_FETCHERS)}",
        )

    try:
        issues = await fetcher(sid)
    except UpstreamRateLimit as exc:
        # Surface a friendly 429 so the UI can render the message.
        raise HTTPException(
            status_code=429,
            detail=f"{exc.source} is rate-limited ({exc.detail}) — try again later",
        ) from exc

    if not issues:
        raise HTTPException(
            status_code=502,
            detail=f"no issues found at {src} for {sid!r}",
        )

    series.source = src
    series.source_id = sid
    series.expected_issues = "\n".join(issues)
    # Wookieepedia marks unpublished issues with "Cancelled" in the
    # publication-date cell — capture those so they don't drag the
    # series' completion-progress denominator below 100% forever.
    if src == "wookieepedia":
        try:
            canceled = await wookieepedia.get_series_canceled_issues(sid)
        except Exception:
            canceled = []
        series.canceled_issues = "\n".join(canceled) if canceled else None
    session.add(series)
    await session.commit()

    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/series/{series_id}/auto-link")
async def series_auto_link(
    series_id: int,
    session: SessionDep,
) -> Response:
    """One-click "figure out the source and pull issues" for legacy
    series rows that pre-date the save-time auto-enrichment in
    /add/save.

    Strategy, in order:
      1. If the Series already has source + source_id, just re-run the
         refresh logic against it.
      2. Otherwise look at the comics in this series. The most common
         (source, source_id) tuple on those comics tells us where the
         data came from. For Wookieepedia we use the series NAME as the
         series-article title; for CV/Metron we'd need to refetch the
         issue to get the volume/series id, which we do lazily.

    Returns 204 + HX-Refresh on success, 422 with an HTMX-banner-able
    body when we can't figure out a source. The UI's existing manual
    form is still available as the fallback.
    """
    series = await session.get(Series, series_id)
    if series is None:
        raise HTTPException(status_code=404, detail="series not found")

    # Step 1: if we already have a source, use it.
    src = (series.source or "").strip()
    sid = (series.source_id or "").strip()

    # Step 2: otherwise sniff the children.
    if not src:
        # Vote by frequency. Most series have a single dominant source,
        # but mixed-source series can happen after CSV imports — pick
        # the one with the most comics behind it.
        rows = (await session.exec(
            select(Comic.source, func.count(Comic.id))
            .where(Comic.series_id == series_id, Comic.source.is_not(None))
            .group_by(Comic.source)
            .order_by(func.count(Comic.id).desc())
        )).all()
        if rows:
            top = rows[0]
            src = (top[0] or "").strip() if isinstance(top, tuple) else (top.source or "").strip()

    if not src:
        raise HTTPException(
            status_code=422,
            detail="Couldn't auto-detect a source for this series — "
                   "none of its comics carry a source. Use the manual "
                   "form to specify one.",
        )

    # Track whether we derived sid from a high-confidence source
    # (a child comic's actual wiki infobox) vs the lower-confidence
    # `series.name` fallback. The year-disambiguation block below
    # only runs against the low-confidence path, to avoid producing
    # plausible-but-wrong matches like landing on "Series (2023)" when
    # the right answer was "Series (2021)".
    sid_from_child_refetch = False

    # Derive a series-level source_id when one wasn't stored.
    if not sid:
        if src == "wookieepedia":
            # Don't trust `series.name` blindly. Legacy series rows
            # often have a too-broad franchise name (e.g. "Star Wars:
            # The High Republic") because of an earlier
            # series-infobox-parsing bug that picked the level-1 bullet
            # instead of the more-specific level-2. Refetch a child
            # comic and read the (now-correctly-parsed) `candidate.series`
            # field for the canonical series-article title. Falls back
            # to `series.name` only when no child comic has a
            # wookieepedia source_id we can dereference.
            child = (await session.exec(
                select(Comic)
                .where(
                    Comic.series_id == series_id,
                    Comic.source == "wookieepedia",
                    Comic.source_id.is_not(None),
                )
                .limit(1)
            )).first()
            if child and child.source_id:
                try:
                    cand = await wookieepedia.get_article(child.source_id)
                except Exception:
                    cand = None
                if cand and cand.series:
                    # `series_article_id` encodes the upstream article
                    # identifier (e.g. "Epic Collection#Legends" for EC
                    # sub-imprints). When absent, the display name IS
                    # the article title.
                    sid = cand.series_article_id or cand.series
                    sid_from_child_refetch = True
                    # Backfill rename: if the refetched candidate has
                    # a more-specific series name than what's stored
                    # (the common case for legacy rows after the
                    # sub-imprint detector landed), adopt it. Collision
                    # with an existing series → merge into that one.
                    if cand.series != series.name:
                        from sqlalchemy import update as sa_update
                        target = (await session.exec(
                            select(Series).where(Series.name == cand.series)
                        )).first()
                        if target and target.id != series.id:
                            # Merge our series into the existing target.
                            old_id = series.id
                            await session.exec(
                                sa_update(Comic)
                                .where(Comic.series_id == old_id)
                                .values(series_id=target.id)
                            )
                            await session.delete(series)
                            await session.commit()
                            series = target
                            series_id = target.id
                        else:
                            series.name = cand.series
                            session.add(series)
                            await session.commit()
            if not sid:
                sid = series.name
        elif src == "comicvine":
            # Refetch one child comic's issue payload to read the
            # volume id off it. We pick any comic with a source_id
            # set; the volume should be the same across the series.
            child = (await session.exec(
                select(Comic)
                .where(
                    Comic.series_id == series_id,
                    Comic.source == "comicvine",
                    Comic.source_id.is_not(None),
                )
                .limit(1)
            )).first()
            if child and child.source_id:
                try:
                    cand = await comicvine.get_issue(child.source_id)
                except Exception:
                    cand = None
                if cand and cand.raw:
                    vol = (cand.raw.get("volume") or {}).get("id")
                    if vol is not None:
                        sid = str(vol)
        elif src == "metron":
            child = (await session.exec(
                select(Comic)
                .where(
                    Comic.series_id == series_id,
                    Comic.source == "metron",
                    Comic.source_id.is_not(None),
                )
                .limit(1)
            )).first()
            if child and child.source_id:
                try:
                    cand = await metron.get_issue(child.source_id)
                except Exception:
                    cand = None
                if cand and cand.raw:
                    ser = cand.raw.get("series") or {}
                    s_id = ser.get("id")
                    if s_id is not None:
                        sid = str(s_id)

    if not sid:
        raise HTTPException(
            status_code=422,
            detail=f"Couldn't derive a {src} series ID automatically. "
                   "Use the manual form to specify one.",
        )

    # Now run the same code as /series/{id}/refresh, but with the
    # values we just figured out.
    fetcher = _FETCHERS.get(src)
    if fetcher is None:
        raise HTTPException(status_code=400, detail=f"unsupported source {src!r}")

    try:
        issues = await fetcher(sid)
    except UpstreamRateLimit as exc:
        raise HTTPException(
            status_code=429,
            detail=f"{exc.source} is rate-limited ({exc.detail}) — try again later",
        ) from exc

    # Wookieepedia-specific fallback: the bare series name often points
    # at a franchise / TV-show / publishing-initiative overview article
    # (e.g. "Star Wars: The High Republic" is the multi-media franchise
    # page; the actual comic series lives at "Star Wars: The High
    # Republic (2021)"). When the bare-name fetch is empty, try the
    # year-disambiguated form using the earliest cover-date year of
    # any owned comic in this series, plus a couple of nearby years to
    # cover off-by-one launches (cover-date Jan often = pub-date Dec
    # of the prior year).
    if not issues and src == "wookieepedia" and not sid_from_child_refetch:
        year_row = (await session.exec(
            select(func.min(func.strftime("%Y", Comic.cover_date)))
            .where(
                Comic.series_id == series_id,
                Comic.cover_date.is_not(None),
            )
        )).first()
        anchor = year_row[0] if isinstance(year_row, tuple) else year_row
        try:
            anchor_year = int(anchor) if anchor else None
        except (TypeError, ValueError):
            anchor_year = None
        if anchor_year is not None:
            tried: list[str] = []
            # Try the exact year first, then ±1. Wookieepedia uses the
            # *first publication year* for the disambiguator, but
            # cover-date can lag by a month or two so the next-younger
            # year is the second-most-likely match.
            for offset in (0, -1, 1, -2, 2):
                candidate = f"{sid} ({anchor_year + offset})"
                if candidate in tried:
                    continue
                tried.append(candidate)
                try:
                    candidate_issues = await fetcher(candidate)
                except UpstreamRateLimit:
                    break
                if candidate_issues:
                    sid = candidate
                    issues = candidate_issues
                    break

    if not issues:
        raise HTTPException(
            status_code=502,
            detail=f"No issues found at {src} for {sid!r}. The wiki "
                   "article might use a different title — try the "
                   "manual form.",
        )

    series.source = src
    series.source_id = sid
    series.expected_issues = "\n".join(issues)
    # Pull the canceled-issues subset too so the progress bar
    # ignores planned-but-unpublished issues.
    if src == "wookieepedia":
        try:
            canceled = await wookieepedia.get_series_canceled_issues(sid)
        except Exception:
            canceled = []
        series.canceled_issues = "\n".join(canceled) if canceled else None
    session.add(series)
    await session.commit()

    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/series/{series_id}/delete")
async def series_delete(
    series_id: int, session: SessionDep,
    confirm: str = Form(default=""),
) -> Response:
    """Delete a Series row plus every link that points at it.

    Comics that had this series as their primary lose the FK (set to
    NULL); comics linked via ComicSeries simply drop the link. The
    comics themselves stay in the library — only the series
    classification goes away.

    Server-side `confirm=yes` belt-and-braces: the UI fires a JS
    confirm() too, but a misconfigured curl shouldn't be able to
    nuke a series without explicit intent.
    """
    series = await session.get(Series, series_id)
    if series is None:
        raise HTTPException(status_code=404, detail="series not found")
    if confirm != "yes":
        raise HTTPException(
            status_code=422,
            detail="Missing confirm=yes; deletion requires explicit confirmation.",
        )

    from sqlalchemy import delete as sa_delete, update as sa_update
    from app.models import ComicSeries

    # 1. Clear primary FK on any comic that was using this as primary.
    await session.exec(
        sa_update(Comic)
        .where(Comic.series_id == series_id)
        .values(series_id=None)
    )
    # 2. Drop every link-table row pointing at this series.
    await session.exec(
        sa_delete(ComicSeries).where(ComicSeries.series_id == series_id)
    )
    # 3. Drop the series itself.
    await session.delete(series)
    await session.commit()

    return Response(status_code=204, headers={"HX-Redirect": "/series"})


@router.post("/series/{series_id}/merge")
async def series_merge(
    series_id: int,
    session: SessionDep,
    target_id: int = Form(...),
) -> Response:
    """Collapse `series_id` into `target_id`. Every comic currently
    pointing at the source is reassigned to the target; the source row is
    then deleted. Source-derived fields on the target (publisher_id,
    source/source_id, expected_issues) are filled from the source ONLY
    when the target has nothing there — never overwriting good data.
    """
    if series_id == target_id:
        raise HTTPException(status_code=400, detail="cannot merge a series into itself")

    source = await session.get(Series, series_id)
    target = await session.get(Series, target_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source series not found")
    if target is None:
        raise HTTPException(status_code=404, detail="target series not found")

    # Adopt source-only metadata on the target.
    if target.publisher_id is None and source.publisher_id is not None:
        target.publisher_id = source.publisher_id
    if not target.source and source.source:
        target.source = source.source
        target.source_id = source.source_id
    if not target.expected_issues and source.expected_issues:
        target.expected_issues = source.expected_issues

    # Reassign every comic in one bulk UPDATE.
    await session.exec(
        update(Comic)
        .where(Comic.series_id == series_id)
        .values(series_id=target_id, updated_at=datetime.now(UTC))
    )
    # Move every multi-series link from source to target. Without
    # this, the ComicSeries table holds dangling references to the
    # deleted source row — which then mislead the orphan-prune
    # logic on comic deletion. INSERT OR IGNORE handles the case
    # where a comic is already linked to both source and target.
    from sqlalchemy import text as _text
    from app.models import ComicSeries
    await session.exec(_text(
        "INSERT OR IGNORE INTO comicseries "
        "  (comic_id, series_id, is_primary, created_at) "
        "SELECT comic_id, :tgt, is_primary, created_at "
        "FROM comicseries WHERE series_id = :src"
    ).bindparams(tgt=target_id, src=series_id))
    from sqlalchemy import delete as sa_delete
    await session.exec(
        sa_delete(ComicSeries).where(ComicSeries.series_id == series_id)
    )
    session.add(target)
    await session.delete(source)
    await session.commit()

    # HTMX picks the redirect up; full-page flow falls back to a 303.
    return Response(
        status_code=204,
        headers={"HX-Redirect": f"/series/{target_id}"},
    )


# ---------------------------------------------------------------------------
# Series library — `GET /series` index
# ---------------------------------------------------------------------------

_STATUS_VALUES = ("complete", "in_progress", "untracked")
_SORT_VALUES = (
    "name_asc", "name_desc",
    "count_desc", "count_asc",
    "completion_desc", "completion_asc",
    "added_desc", "added_asc",
)
_GROUP_VALUES = ("none", "publisher", "fandom", "status")


def _drop_empty(values: list[str]) -> list[str]:
    return [v for v in (values or []) if v]


async def _bulk_comic_counts(session: AsyncSession, series_ids: list[int]) -> dict[int, int]:
    if not series_ids:
        return {}
    rows = (await session.exec(
        select(Comic.series_id, func.count(Comic.id))
        .where(Comic.series_id.in_(series_ids))
        .group_by(Comic.series_id)
    )).all()
    return {sid: int(n) for (sid, n) in rows}


async def _bulk_fandom_mode(session: AsyncSession, series_ids: list[int]) -> dict[int, str]:
    """Mode of `Comic.fandom` per series — i.e. the fandom that the largest
    share of its comics carry. Used for the fandom filter facet and the
    small badge on each series card."""
    if not series_ids:
        return {}
    rows = (await session.exec(
        select(Comic.series_id, Comic.fandom)
        .where(Comic.series_id.in_(series_ids), Comic.fandom.is_not(None))
    )).all()
    by_series: dict[int, Counter] = defaultdict(Counter)
    for sid, fandom in rows:
        by_series[sid][fandom] += 1
    return {sid: c.most_common(1)[0][0] for sid, c in by_series.items() if c}


async def _bulk_series_covers(
    session: AsyncSession, series_ids: list[int], *, per_series: int = 4,
) -> dict[int, list[str]]:
    """Up to N owned-comic covers per series, ordered by cover date / id.
    Drives the collage on each series card when `Series.cover_url` isn't
    set on the row itself.

    Pulls comics via BOTH the primary `series_id` FK AND the
    ComicSeries multi-series link table. Inferred series — where the
    only comic that "belongs" is an omnibus linked via the link table
    — would otherwise render with an empty "No covers yet" placeholder
    even though the user owns a comic that should display there.
    """
    if not series_ids:
        return {}
    from app.models import ComicSeries
    out: dict[int, list[str]] = defaultdict(list)
    seen: dict[int, set[int]] = defaultdict(set)  # series_id → {comic_id}

    def _absorb(rows):
        for sid, cid, local, remote in rows:
            url = local or remote
            if not url:
                continue
            if cid in seen[sid]:
                continue
            if len(out[sid]) >= per_series:
                continue
            seen[sid].add(cid)
            out[sid].append(url)

    # Primary FK path.
    primary = (await session.exec(
        select(
            Comic.series_id, Comic.id,
            Comic.cover_url_local, Comic.cover_url_remote,
        )
        .where(
            Comic.series_id.in_(series_ids),
            or_(Comic.cover_url_local.is_not(None), Comic.cover_url_remote.is_not(None)),
        )
        .order_by(
            Comic.series_id.asc(),
            Comic.cover_date.asc().nullslast(),
            Comic.id.asc(),
        )
    )).all()
    _absorb(primary)

    # Link-table path.
    linked = (await session.exec(
        select(
            ComicSeries.series_id, Comic.id,
            Comic.cover_url_local, Comic.cover_url_remote,
        )
        .join(Comic, Comic.id == ComicSeries.comic_id)
        .where(
            ComicSeries.series_id.in_(series_ids),
            or_(Comic.cover_url_local.is_not(None), Comic.cover_url_remote.is_not(None)),
        )
        .order_by(
            ComicSeries.series_id.asc(),
            Comic.cover_date.asc().nullslast(),
            Comic.id.asc(),
        )
    )).all()
    _absorb(linked)

    return dict(out)


def _series_status(progress) -> str:
    """Map a `Progress` (or None) onto our three top-level buckets."""
    if progress is None or progress.total == 0:
        return "untracked"
    return "complete" if progress.is_complete else "in_progress"


def _sort_rows(rows: list[dict], how: str) -> list[dict]:
    def _name(r):
        return (r["series"].name or "").lower()
    if how == "name_desc":
        return sorted(rows, key=_name, reverse=True)
    if how == "count_desc":
        return sorted(rows, key=lambda r: (-r["comic_count"], _name(r)))
    if how == "count_asc":
        return sorted(rows, key=lambda r: (r["comic_count"], _name(r)))
    if how == "completion_desc":
        return sorted(rows, key=lambda r: (
            -(r["progress_pct"] if r["progress_pct"] is not None else -1),
            -r["comic_count"], _name(r),
        ))
    if how == "completion_asc":
        # Untracked (no progress) goes last; among tracked, least-complete first.
        return sorted(rows, key=lambda r: (
            r["progress_pct"] if r["progress_pct"] is not None else 101,
            -r["comic_count"], _name(r),
        ))
    if how == "added_desc":
        # "Recently added" → series with the highest id (latest created).
        return sorted(rows, key=lambda r: (-r["series"].id, _name(r)))
    if how == "added_asc":
        return sorted(rows, key=lambda r: (r["series"].id, _name(r)))
    return sorted(rows, key=_name)


def _group_series_rows(rows: list[dict], how: str) -> list[dict]:
    """Bucket the series rows by publisher / fandom / status (or
    pass through ungrouped). Returns a list of `{label, items}` dicts
    mirroring the library grid's grouping shape so the template can
    reuse the same render pattern."""
    if how == "none" or not rows:
        return [{"label": None, "items": rows}]
    buckets: dict[str, list[dict]] = {}
    for r in rows:
        if how == "publisher":
            label = r["publisher"].name if r.get("publisher") else "(no publisher)"
        elif how == "fandom":
            label = (r.get("fandom") or "(no fandom)").title()
        elif how == "status":
            label = {
                "complete": "Complete",
                "in_progress": "In progress",
                "untracked": "Untracked",
            }.get(r["status"], r["status"])
        else:
            label = "(other)"
        buckets.setdefault(label, []).append(r)
    return [{"label": k, "items": v} for k, v in sorted(buckets.items(), key=lambda kv: kv[0] or "")]


async def _build_series_index(
    session: AsyncSession,
    *,
    publishers: list[str],
    fandoms: list[str],
    statuses: list[str],
    q: str,
    sort: str,
    page: int,
    page_size: int,
    group: str = "none",
) -> dict:
    """Filter → enrich → status-filter → sort → paginate.

    Returns a context dict ready to feed `series_index.html` (and the
    `_series_grid.html` partial during HTMX swaps)."""
    base = select(Series, Publisher).join(
        Publisher, Publisher.id == Series.publisher_id, isouter=True,
    )
    if publishers:
        base = base.where(Publisher.name.in_(publishers))
    if q:
        base = base.where(Series.name.ilike(f"%{q}%"))
    if fandoms:
        sub = select(Comic.series_id).where(Comic.fandom.in_(fandoms))
        base = base.where(Series.id.in_(sub))
    rows = (await session.exec(base.order_by(Series.name.asc()))).all()

    series_ids = [s.id for (s, _p) in rows]
    counts = await _bulk_comic_counts(session, series_ids)
    fandom_mode = await _bulk_fandom_mode(session, series_ids)
    progress_by_id = await compute_progress(session, series_ids)

    enriched: list[dict] = []
    for s, pub in rows:
        prog = progress_by_id.get(s.id)
        enriched.append({
            "series": s,
            "publisher": pub,
            "comic_count": counts.get(s.id, 0),
            "fandom": fandom_mode.get(s.id),
            "progress": prog,
            "progress_pct": prog.pct if prog else None,
            "owned": prog.owned if prog else 0,
            "expected_total": prog.total if prog else 0,
            "is_complete": bool(prog and prog.is_complete),
            "status": _series_status(prog),
        })

    if statuses:
        wanted = set(statuses)
        enriched = [r for r in enriched if r["status"] in wanted]

    enriched = _sort_rows(enriched, sort)

    total = len(enriched)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(page, page_count)
    start = (page - 1) * page_size
    page_rows = enriched[start:start + page_size]

    page_ids = [r["series"].id for r in page_rows]
    covers_by_id = await _bulk_series_covers(session, page_ids, per_series=4)
    for r in page_rows:
        r["cover_urls"] = covers_by_id.get(r["series"].id, [])

    # Bucket the page rows by the requested grouping. The template
    # renders `groups` as a list of `{label, items}` sections; for the
    # `none` mode it's a single section with `label=None`.
    groups = _group_series_rows(page_rows, group)

    pub_facets: Counter = Counter()
    fan_facets: Counter = Counter()
    status_facets: Counter = Counter()
    for r in enriched:
        if r["publisher"]:
            pub_facets[r["publisher"].name] += 1
        if r["fandom"]:
            fan_facets[r["fandom"]] += 1
        status_facets[r["status"]] += 1

    return {
        "rows": page_rows,
        "total": total,
        "page": page,
        "page_count": page_count,
        "page_size": page_size,
        "groups": groups,
        "selected": {
            "publishers": list(publishers),
            "fandoms": list(fandoms),
            "statuses": list(statuses),
            "q": q,
            "sort": sort,
            "group": group,
        },
        "facets": {
            "publishers": pub_facets.most_common(),
            "fandoms": fan_facets.most_common(),
            "statuses": [(k, status_facets.get(k, 0)) for k in _STATUS_VALUES],
        },
    }


_INDEX_HELPERS_DEFINED_BELOW = True  # see file footer
# (Index endpoints `/series` + `/series/grid` are registered earlier in
#  the file — above the `/series/{series_id}` route — so FastAPI's path
#  matcher reaches the static `/series/grid` URL first. The handler
#  bodies live above; helpers below stay where they are.)
