"""Library view.

GET /library  — full page (header, filter sidebar, grid).
GET /library/grid — partial: just the grid + paginator (HTMX-swappable).
POST /library/bulk — bulk-edit a set of comic IDs in one shot.

Filters (all repeatable query params):
  - publisher
  - series
  - year   (cover_date year)
  - q      (substring on title)
Grouping: group=none|series|publisher|year (default none = flat grid).
Pagination: page, page_size (default 24, capped at 100).
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Optional

from datetime import UTC, datetime
from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import extract, func, or_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import Comic, ComicArc, ComicTag, Copy, Publisher, Series, StoryArc, Tag
from app.services.series_progress import compute_progress

router = APIRouter(tags=["library"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]

GROUP_VALUES = {"none", "series", "publisher", "year"}

# User-facing sort modes for the library grid. Maps to ORDER BY
# clauses inside `_query_page`. `added_desc` is the legacy default
# (newest comic first); keep it at the top of the dropdown so the
# default behaviour doesn't change.
SORT_VALUES = {
    "added_desc",       # legacy: Comic.id DESC (proxy for date added)
    "added_asc",
    "title_asc",
    "title_desc",
    "cover_date_asc",
    "cover_date_desc",
    "series_asc",       # by series name, then issue_number for stable order
}


def _drop_empty(values: list[str]) -> list[str]:
    """Filter out empty-string entries — they come from "All …" dropdown
    options and should be treated as 'no filter on this field'."""
    return [v for v in (values or []) if v]


def _apply_filters(
    stmt, *, publishers, series_names, years, tag_names, q,
    formats=(), canons=(), eras=(), arcs=(),
    read_statuses=(), storages=(), fandoms=(),
    include_tracked: bool = False,
):
    # Stub Comics (no Copy rows) are tracked-but-not-owned — e.g. a
    # TPB the user marked as contained inside an omnibus they own. By
    # default the library hides them so the page reflects the
    # physical collection; `include_tracked` flips the gate.
    if not include_tracked:
        owned_sub = select(Copy.comic_id).distinct()
        stmt = stmt.where(Comic.id.in_(owned_sub))
    if publishers:
        stmt = stmt.where(Publisher.name.in_(publishers))
    if series_names:
        stmt = stmt.where(Series.name.in_(series_names))
    if years:
        stmt = stmt.where(extract("year", Comic.cover_date).in_(years))
    if formats:
        stmt = stmt.where(Comic.format.in_(formats))
    if canons:
        stmt = stmt.where(Comic.canon.in_(canons))
    if eras:
        stmt = stmt.where(Comic.era.in_(eras))
    if fandoms:
        stmt = stmt.where(Comic.fandom.in_(fandoms))
    if tag_names:
        # Restrict to comics that have at least one of the selected tags.
        sub = (
            select(ComicTag.comic_id)
            .join(Tag, Tag.id == ComicTag.tag_id)
            .where(Tag.name.in_(tag_names))
        )
        stmt = stmt.where(Comic.id.in_(sub))
    if arcs:
        sub = (
            select(ComicArc.comic_id)
            .join(StoryArc, StoryArc.id == ComicArc.arc_id)
            .where(StoryArc.name.in_(arcs))
        )
        stmt = stmt.where(Comic.id.in_(sub))
    if read_statuses:
        sub = select(Copy.comic_id).where(Copy.read_status.in_(read_statuses))
        stmt = stmt.where(Comic.id.in_(sub))
    if storages:
        sub = select(Copy.comic_id).where(Copy.storage_location.in_(storages))
        stmt = stmt.where(Comic.id.in_(sub))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Comic.title.ilike(like), Series.name.ilike(like)))
    return stmt


async def _facets(
    session: AsyncSession, *,
    publishers, series_names, years, tag_names, q,
    formats=(), canons=(), eras=(), arcs=(),
    read_statuses=(), storages=(), fandoms=(),
) -> dict:
    """Counts grouped by publisher / series / year / tag / format / canon / era
    over the filter set itself. Each facet group is computed against the
    *currently applied* filters minus that group, so the user can refine
    within one group.
    """

    async def _count(group_col, *, exclude=None, extra_join=None):
        base = (
            select(group_col, func.count(func.distinct(Comic.id)))
            .select_from(Comic)
            .join(Series, Series.id == Comic.series_id, isouter=True)
            .join(Publisher, Publisher.id == Series.publisher_id, isouter=True)
        )
        if extra_join == "tag":
            base = base.join(ComicTag, ComicTag.comic_id == Comic.id, isouter=True)
            base = base.join(Tag, Tag.id == ComicTag.tag_id, isouter=True)
        elif extra_join == "arc":
            base = base.join(ComicArc, ComicArc.comic_id == Comic.id, isouter=True)
            base = base.join(StoryArc, StoryArc.id == ComicArc.arc_id, isouter=True)
        elif extra_join == "copy":
            base = base.join(Copy, Copy.comic_id == Comic.id, isouter=True)
        base = _apply_filters(
            base,
            publishers=publishers if exclude != "publisher" else [],
            series_names=series_names if exclude != "series" else [],
            years=years if exclude != "year" else [],
            tag_names=tag_names if exclude != "tag" else [],
            formats=formats if exclude != "format" else (),
            canons=canons if exclude != "canon" else (),
            eras=eras if exclude != "era" else (),
            arcs=arcs if exclude != "arc" else (),
            read_statuses=read_statuses if exclude != "read_status" else (),
            storages=storages if exclude != "storage" else (),
            fandoms=fandoms if exclude != "fandom" else (),
            q=q,
        )
        base = base.where(group_col.is_not(None)).group_by(group_col).order_by(func.count(func.distinct(Comic.id)).desc()).limit(50)
        result = await session.exec(base)
        return [(row[0], row[1]) for row in result.all()]

    pubs = await _count(Publisher.name, exclude="publisher")
    srss = await _count(Series.name, exclude="series")
    yrs = await _count(extract("year", Comic.cover_date), exclude="year")
    # Years read more naturally newest → oldest than by count. The _count
    # helper sorts by count desc; re-sort here so the UI is predictable.
    yrs = sorted(yrs, key=lambda r: int(r[0]) if r[0] is not None else 0, reverse=True)
    tgs = await _count(Tag.name, exclude="tag", extra_join="tag")
    fmts = await _count(Comic.format, exclude="format")
    cns = await _count(Comic.canon, exclude="canon")
    ers = await _count(Comic.era, exclude="era")
    arc_counts = await _count(StoryArc.name, exclude="arc", extra_join="arc")
    read_counts = await _count(Copy.read_status, exclude="read_status", extra_join="copy")
    storage_counts = await _count(Copy.storage_location, exclude="storage", extra_join="copy")
    fandom_counts = await _count(Comic.fandom, exclude="fandom")
    return {
        "publishers": pubs, "series": srss, "years": yrs, "tags": tgs,
        "formats": fmts, "canons": cns, "eras": ers, "arcs": arc_counts,
        "read_statuses": read_counts, "storages": storage_counts,
        "fandoms": fandom_counts,
    }


async def _query_page(
    session: AsyncSession,
    *,
    publishers,
    series_names,
    years,
    tag_names,
    q,
    page,
    page_size,
    formats=(),
    canons=(),
    eras=(),
    arcs=(),
    read_statuses=(),
    storages=(),
    fandoms=(),
    include_tracked: bool = False,
    sort: str = "added_desc",
):
    base = (
        select(Comic, Series, Publisher)
        .select_from(Comic)
        .join(Series, Series.id == Comic.series_id, isouter=True)
        .join(Publisher, Publisher.id == Series.publisher_id, isouter=True)
    )
    base = _apply_filters(
        base, publishers=publishers, series_names=series_names,
        years=years, tag_names=tag_names, q=q,
        formats=formats, canons=canons, eras=eras, arcs=arcs,
        read_statuses=read_statuses, storages=storages, fandoms=fandoms,
        include_tracked=include_tracked,
    )

    count_stmt = (
        select(func.count(Comic.id))
        .select_from(Comic)
        .join(Series, Series.id == Comic.series_id, isouter=True)
        .join(Publisher, Publisher.id == Series.publisher_id, isouter=True)
    )
    count_stmt = _apply_filters(
        count_stmt, publishers=publishers, series_names=series_names,
        years=years, tag_names=tag_names, q=q,
        formats=formats, canons=canons, eras=eras, arcs=arcs,
        read_statuses=read_statuses, storages=storages, fandoms=fandoms,
        include_tracked=include_tracked,
    )
    total = (await session.exec(count_stmt)).first() or 0

    page = max(page, 1)
    offset = (page - 1) * page_size
    # Resolve the sort mode. `added_desc` is the legacy default; the
    # tie-break is always Comic.id DESC so two comics with the same
    # cover_date / title don't shuffle between page renders.
    sort_clauses_map = {
        "added_desc":      [Comic.id.desc()],
        "added_asc":       [Comic.id.asc()],
        "title_asc":       [Comic.title.asc().nullslast(), Comic.id.desc()],
        "title_desc":      [Comic.title.desc().nullslast(), Comic.id.desc()],
        "cover_date_asc":  [Comic.cover_date.asc().nullslast(), Comic.id.desc()],
        "cover_date_desc": [Comic.cover_date.desc().nullslast(), Comic.id.desc()],
        "series_asc":      [Series.name.asc().nullslast(),
                            Comic.issue_number.asc().nullslast(),
                            Comic.id.desc()],
    }
    order_clauses = sort_clauses_map.get(sort, sort_clauses_map["added_desc"])
    stmt = base.order_by(*order_clauses).offset(offset).limit(page_size)
    rows = (await session.exec(stmt)).all()
    items = [
        {"comic": comic, "series": ser, "publisher": pub}
        for (comic, ser, pub) in rows
    ]

    # Per-comic copy counts in one query.
    comic_ids = [it["comic"].id for it in items]
    counts: dict[int, int] = {}
    if comic_ids:
        cstmt = (
            select(Copy.comic_id, func.count(Copy.id))
            .where(Copy.comic_id.in_(comic_ids))
            .group_by(Copy.comic_id)
        )
        for cid, n in (await session.exec(cstmt)).all():
            counts[cid] = n
    for it in items:
        it["copies"] = counts.get(it["comic"].id, 0)

    # Per-series completion progress, only for series with a refreshed
    # issue list. Comics whose series isn't in this dict simply render
    # without a progress bar.
    series_ids = [it["comic"].series_id for it in items if it["comic"].series_id]
    progress = await compute_progress(session, list(set(series_ids)))
    for it in items:
        sid = it["comic"].series_id
        it["progress"] = progress.get(sid) if sid else None

    return items, int(total)


def _group_items(items: list[dict], group: str) -> list[dict]:
    if group == "none" or not items:
        return [{"label": None, "items": items}]
    buckets: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        if group == "series":
            label = (it["series"].name if it["series"] else "(no series)")
        elif group == "publisher":
            label = (it["publisher"].name if it["publisher"] else "(no publisher)")
        elif group == "year":
            cd = it["comic"].cover_date
            label = str(cd.year) if cd else "(no year)"
        else:
            label = ""
        buckets[label].append(it)
    return [{"label": k, "items": v} for k, v in sorted(buckets.items(), key=lambda kv: kv[0] or "")]


def _grid_context(
    *,
    items,
    total,
    page,
    page_size,
    publishers,
    series_names,
    years,
    tag_names,
    q,
    group,
    facets,
    formats=(),
    canons=(),
    eras=(),
    arcs=(),
    read_statuses=(),
    storages=(),
    fandoms=(),
) -> dict:
    return {
        "groups": _group_items(items, group),
        "total": total,
        "page": page,
        "page_size": page_size,
        "page_count": max(1, (total + page_size - 1) // page_size),
        "selected": {
            "publishers": list(publishers),
            "series": list(series_names),
            "years": [int(y) for y in years],
            "tags": list(tag_names),
            "formats": list(formats),
            "canons": list(canons),
            "eras": list(eras),
            "arcs": list(arcs),
            "read_statuses": list(read_statuses),
            "storages": list(storages),
            "fandoms": list(fandoms),
            "q": q,
            "group": group,
        },
        "facets": facets,
    }


@router.get("/library", response_class=HTMLResponse)
async def library_page(
    request: Request,
    session: SessionDep,
    publisher: list[str] = Query(default=[]),
    series: list[str] = Query(default=[]),
    year: list[int] = Query(default=[]),
    tag: list[str] = Query(default=[]),
    format: list[str] = Query(default=[]),
    canon: list[str] = Query(default=[]),
    era: list[str] = Query(default=[]),
    arc: list[str] = Query(default=[]),
    read_status: list[str] = Query(default=[]),
    storage: list[str] = Query(default=[]),
    fandom: list[str] = Query(default=[]),
    q: str = Query(default=""),
    group: str = Query(default="none"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
    tracked: str = Query(default=""),
    sort: str = Query(default="added_desc"),
) -> HTMLResponse:
    if group not in GROUP_VALUES:
        group = "none"
    publisher = _drop_empty(publisher)
    series = _drop_empty(series)
    tag = _drop_empty(tag)
    format = _drop_empty(format)
    canon = _drop_empty(canon)
    era = _drop_empty(era)
    arc = _drop_empty(arc)
    read_status = _drop_empty(read_status)
    storage = _drop_empty(storage)
    fandom = _drop_empty(fandom)
    include_tracked = tracked.lower() in ("1", "true", "on", "yes")
    if sort not in SORT_VALUES:
        sort = "added_desc"
    items, total = await _query_page(
        session,
        publishers=publisher,
        series_names=series,
        years=year,
        tag_names=tag,
        q=q,
        page=page,
        page_size=page_size,
        formats=format,
        canons=canon,
        eras=era,
        arcs=arc,
        read_statuses=read_status,
        storages=storage,
        fandoms=fandom,
        include_tracked=include_tracked,
        sort=sort,
    )
    facets = await _facets(
        session, publishers=publisher, series_names=series,
        years=year, tag_names=tag, q=q,
        formats=format, canons=canon, eras=era, arcs=arc,
        read_statuses=read_status, storages=storage, fandoms=fandom,
    )
    ctx = _grid_context(
        items=items, total=total, page=page, page_size=page_size,
        publishers=publisher, series_names=series, years=year,
        tag_names=tag, q=q, group=group, facets=facets,
        formats=format, canons=canon, eras=era, arcs=arc,
        read_statuses=read_status, storages=storage, fandoms=fandom,
    )
    ctx["include_tracked"] = include_tracked
    ctx["sort"] = sort
    return templates.TemplateResponse(request, "library.html", ctx)


@router.get("/library/grid", response_class=HTMLResponse)
async def library_grid(
    request: Request,
    session: SessionDep,
    publisher: list[str] = Query(default=[]),
    series: list[str] = Query(default=[]),
    year: list[int] = Query(default=[]),
    tag: list[str] = Query(default=[]),
    format: list[str] = Query(default=[]),
    canon: list[str] = Query(default=[]),
    era: list[str] = Query(default=[]),
    arc: list[str] = Query(default=[]),
    read_status: list[str] = Query(default=[]),
    storage: list[str] = Query(default=[]),
    fandom: list[str] = Query(default=[]),
    q: str = Query(default=""),
    group: str = Query(default="none"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
    tracked: str = Query(default=""),
    sort: str = Query(default="added_desc"),
) -> HTMLResponse:
    if group not in GROUP_VALUES:
        group = "none"
    publisher = _drop_empty(publisher)
    series = _drop_empty(series)
    tag = _drop_empty(tag)
    format = _drop_empty(format)
    canon = _drop_empty(canon)
    era = _drop_empty(era)
    arc = _drop_empty(arc)
    read_status = _drop_empty(read_status)
    storage = _drop_empty(storage)
    fandom = _drop_empty(fandom)
    include_tracked = tracked.lower() in ("1", "true", "on", "yes")
    if sort not in SORT_VALUES:
        sort = "added_desc"
    items, total = await _query_page(
        session,
        publishers=publisher,
        series_names=series,
        years=year,
        tag_names=tag,
        q=q,
        page=page,
        page_size=page_size,
        formats=format,
        canons=canon,
        eras=era,
        arcs=arc,
        read_statuses=read_status,
        storages=storage,
        fandoms=fandom,
        include_tracked=include_tracked,
        sort=sort,
    )
    ctx = _grid_context(
        items=items, total=total, page=page, page_size=page_size,
        publishers=publisher, series_names=series, years=year,
        tag_names=tag, q=q, group=group,
        formats=format, canons=canon, eras=era, arcs=arc,
        read_statuses=read_status, storages=storage, fandoms=fandom,
        facets={"publishers": [], "series": [], "years": [], "tags": [],
                "formats": [], "canons": [], "eras": [], "arcs": [],
                "read_statuses": [], "storages": [], "fandoms": []},
    )
    return templates.TemplateResponse(request, "partials/_library_grid.html", ctx)


# ---------------------------------------------------------------------------
# Bulk edit
# ---------------------------------------------------------------------------


@router.post("/library/bulk")
async def library_bulk_edit(
    request: Request,
    session: SessionDep,
    comic_id: list[int] = Form(default=[]),
    storage_location: str = Form(default=""),
    format: str = Form(default=""),
    canon: str = Form(default=""),
    era: str = Form(default=""),
    mark_read: str = Form(default=""),
    add_tags: str = Form(default=""),
    remove_tags: str = Form(default=""),
    return_to: str = Form(default="/library"),
) -> RedirectResponse:
    """Apply one or more field changes to every selected comic in one shot.

    Empty form fields are treated as 'don't touch this column' — only fields
    the user explicitly set get written. `storage_location` writes to the
    first Copy of each comic (creating one if missing) since that field
    lives on Copy. `mark_read=on` flips the first not-yet-read copy of each
    comic to `read_status=read, date_read=today`.

    Returns a redirect back to the originating library URL so all current
    filters are preserved.
    """
    if not comic_id:
        return RedirectResponse(url=return_to, status_code=303)

    comics = (
        await session.exec(select(Comic).where(Comic.id.in_(comic_id)))
    ).all()
    today = datetime.now(UTC).date()

    # ── Tag bulk-ops setup ────────────────────────────────────────────────
    # Accept comma- or semicolon-separated lists. Normalize the same way
    # /comic/{id}/tags does so case/whitespace variants don't create dupes.
    def _split_tags(raw: str) -> list[str]:
        names: list[str] = []
        for chunk in re.split(r"[,;]", raw or ""):
            n = re.sub(r"\s+", " ", chunk).strip().lower()
            if n:
                names.append(n)
        return names

    add_names = _split_tags(add_tags)
    remove_names = _split_tags(remove_tags)

    # Resolve / create Tag rows up front so we touch each name once.
    tag_ids_by_name: dict[str, int] = {}
    if add_names or remove_names:
        existing = (await session.exec(
            select(Tag).where(Tag.name.in_(set(add_names + remove_names)))
        )).all()
        for tag in existing:
            tag_ids_by_name[tag.name] = tag.id
        for name in add_names:
            if name not in tag_ids_by_name:
                tag = Tag(name=name)
                session.add(tag)
                await session.flush()
                tag_ids_by_name[name] = tag.id

    add_ids = [tag_ids_by_name[n] for n in add_names if n in tag_ids_by_name]
    remove_ids = [tag_ids_by_name[n] for n in remove_names if n in tag_ids_by_name]

    from app.services.csv_import import translate_format as _norm_format
    normalized_format = _norm_format(format) if format else None
    for comic in comics:
        if normalized_format:
            comic.format = normalized_format
        if canon:
            comic.canon = canon
        if era:
            comic.era = era
        if format or canon or era:
            session.add(comic)

        # Storage and mark-read both touch Copy. Load the comic's copies once.
        if storage_location or mark_read == "on":
            copies = (
                await session.exec(
                    select(Copy).where(Copy.comic_id == comic.id).order_by(Copy.id.asc())
                )
            ).all()

            if storage_location:
                if not copies:
                    new_copy = Copy(comic_id=comic.id, storage_location=storage_location)
                    session.add(new_copy)
                else:
                    # Apply to every copy so the bulk action is predictable;
                    # users splitting copies across boxes can still tweak
                    # individual rows on the detail page.
                    for cp in copies:
                        cp.storage_location = storage_location
                        session.add(cp)

            if mark_read == "on" and copies:
                target = next(
                    (cp for cp in copies if cp.read_status != "read"),
                    None,
                )
                if target is not None:
                    target.read_status = "read"
                    if target.date_read is None:
                        target.date_read = today
                    session.add(target)

        # Tag add/remove. We pre-resolved the IDs above so each comic just
        # needs a couple of upserts/deletes.
        if add_ids:
            existing_links = (await session.exec(
                select(ComicTag.tag_id).where(
                    ComicTag.comic_id == comic.id, ComicTag.tag_id.in_(add_ids),
                )
            )).all()
            already = {t for t in existing_links}
            for tid in add_ids:
                if tid not in already:
                    session.add(ComicTag(comic_id=comic.id, tag_id=tid))
        if remove_ids:
            from sqlalchemy import delete as sa_delete
            await session.exec(
                sa_delete(ComicTag).where(
                    ComicTag.comic_id == comic.id, ComicTag.tag_id.in_(remove_ids),
                )
            )

    await session.commit()
    return RedirectResponse(url=return_to or "/library", status_code=303)


# ---------------------------------------------------------------------------
# Bulk delete
# ---------------------------------------------------------------------------


@router.post("/library/bulk-delete")
async def library_bulk_delete(
    request: Request,
    session: SessionDep,
    comic_id: list[int] = Form(default=[]),
    confirm: str = Form(default=""),
    return_to: str = Form(default="/library"),
) -> RedirectResponse:
    """Delete every selected comic + its copies + its now-orphan series.

    The UI fires this with a JS `confirm(...)` blocker; `confirm=yes` is
    a server-side safety belt so a misconfigured curl can't wipe data
    without explicit intent. Mirrors `/comic/{id}/delete`'s cascade
    semantics so library bulk delete and single delete leave the DB in
    the same shape.
    """
    if not comic_id or confirm != "yes":
        return RedirectResponse(url=return_to or "/library", status_code=303)

    from sqlalchemy import delete as sa_delete

    # Snapshot every series these comics were linked to — primary FK
    # AND the multi-series link table — so we can orphan-prune
    # afterwards. Previously we only snapshotted Comic.series_id,
    # which missed inferred non-primary series; deleting an omnibus
    # left "Star Wars: Knights of the Old Republic (comic series)"
    # behind as a ghost in the library facets.
    from app.models import ComicSeries, ComicContainment, ComicCreator
    series_ids: set[int] = set()
    primary_rows = (
        await session.exec(
            select(Comic.series_id).where(Comic.id.in_(comic_id))
        )
    ).all()
    for r in primary_rows:
        v = r[0] if isinstance(r, tuple) else r
        if v is not None:
            series_ids.add(v)
    link_rows = (
        await session.exec(
            select(ComicSeries.series_id).where(ComicSeries.comic_id.in_(comic_id))
        )
    ).all()
    for r in link_rows:
        v = r[0] if isinstance(r, tuple) else r
        if v is not None:
            series_ids.add(v)

    # Cascade delete: link tables first, then Copy, then Comic.
    await session.exec(sa_delete(ComicTag).where(ComicTag.comic_id.in_(comic_id)))
    await session.exec(sa_delete(ComicArc).where(ComicArc.comic_id.in_(comic_id)))
    await session.exec(sa_delete(ComicCreator).where(ComicCreator.comic_id.in_(comic_id)))
    await session.exec(sa_delete(ComicSeries).where(ComicSeries.comic_id.in_(comic_id)))
    await session.exec(sa_delete(ComicContainment).where(ComicContainment.parent_id.in_(comic_id)))
    await session.exec(sa_delete(ComicContainment).where(ComicContainment.child_id.in_(comic_id)))
    await session.exec(sa_delete(Copy).where(Copy.comic_id.in_(comic_id)))
    await session.exec(sa_delete(Comic).where(Comic.id.in_(comic_id)))
    await session.commit()

    # Prune now-empty series so the library facets don't show ghosts.
    # A series is "empty" when no Comic.series_id and no ComicSeries
    # row references it.
    if series_ids:
        for sid in series_ids:
            primary_n = (await session.exec(
                select(func.count(Comic.id)).where(Comic.series_id == sid)
            )).first()
            primary_n = primary_n[0] if isinstance(primary_n, tuple) else primary_n
            link_n = (await session.exec(
                select(func.count())
                .select_from(ComicSeries)
                .join(Comic, Comic.id == ComicSeries.comic_id)
                .where(ComicSeries.series_id == sid)
            )).first()
            link_n = link_n[0] if isinstance(link_n, tuple) else link_n
            if int(primary_n or 0) == 0 and int(link_n or 0) == 0:
                ghost = await session.get(Series, sid)
                if ghost is not None:
                    await session.delete(ghost)
        await session.commit()

    return RedirectResponse(url=return_to or "/library", status_code=303)
