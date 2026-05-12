"""Global search.

GET /search?q=<query>  →  HTML results page (full-page view).
GET /search/suggest?q=<query>  →  small HTMX dropdown for the header bar.

The query is matched against:

  * Comic.title (substring, case-insensitive)
  * Series.name (substring)
  * Comic identifiers (isbn_13/isbn_10/upc/comicvine_id/metron_id) — exact
  * Creator.name via ComicCreator (substring) → comics by that creator
  * StoryArc.name via ComicArc (substring) → comics in that arc
  * Tag.name via ComicTag (substring) → comics with that tag

Only one query, deduplicated, capped at 100 hits. SQLite LIKE is plenty
fast at the scale we're talking about (thousands of comics, tens of
thousands of joins). FTS5 is overkill for v1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import (
    Comic,
    ComicArc,
    ComicCreator,
    ComicTag,
    Copy,
    Creator,
    Publisher,
    Series,
    StoryArc,
    Tag,
)

router = APIRouter(tags=["search"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]

MAX_RESULTS = 100
SUGGEST_LIMIT = 8


def _comic_query(q: str):
    """Build the WHERE clause that matches any field carrying `q`."""
    like = f"%{q}%"

    creator_sub = (
        select(ComicCreator.comic_id)
        .join(Creator, Creator.id == ComicCreator.creator_id)
        .where(Creator.name.ilike(like))
    )
    arc_sub = (
        select(ComicArc.comic_id)
        .join(StoryArc, StoryArc.id == ComicArc.arc_id)
        .where(StoryArc.name.ilike(like))
    )
    tag_sub = (
        select(ComicTag.comic_id)
        .join(Tag, Tag.id == ComicTag.tag_id)
        .where(Tag.name.ilike(like))
    )

    clauses = [
        Comic.title.ilike(like),
        Series.name.ilike(like),
        Comic.isbn_13 == q,
        Comic.isbn_10 == q,
        Comic.upc == q,
        Comic.comicvine_id == q,
        Comic.metron_id == q,
        Comic.id.in_(creator_sub),
        Comic.id.in_(arc_sub),
        Comic.id.in_(tag_sub),
    ]
    return or_(*clauses)


async def _matching_comics(session: AsyncSession, q: str, limit: int):
    stmt = (
        select(Comic, Series, Publisher)
        .select_from(Comic)
        .join(Series, Series.id == Comic.series_id, isouter=True)
        .join(Publisher, Publisher.id == Series.publisher_id, isouter=True)
        .where(_comic_query(q))
        .order_by(Comic.title)
        .limit(limit)
    )
    rows = (await session.exec(stmt)).all()
    return [
        {"comic": comic, "series": ser, "publisher": pub}
        for (comic, ser, pub) in rows
    ]


async def _matching_creators(session: AsyncSession, q: str):
    """Distinct creators whose name matches — useful as 'jump to all
    comics by this creator' chips."""
    like = f"%{q}%"
    stmt = (
        select(Creator.name, func.count(func.distinct(ComicCreator.comic_id)))
        .join(ComicCreator, ComicCreator.creator_id == Creator.id, isouter=True)
        .where(Creator.name.ilike(like))
        .group_by(Creator.name)
        .order_by(Creator.name)
        .limit(20)
    )
    return [(name, count or 0) for (name, count) in (await session.exec(stmt)).all()]


async def _matching_arcs(session: AsyncSession, q: str):
    like = f"%{q}%"
    stmt = (
        select(StoryArc.name, func.count(func.distinct(ComicArc.comic_id)))
        .join(ComicArc, ComicArc.arc_id == StoryArc.id, isouter=True)
        .where(StoryArc.name.ilike(like))
        .group_by(StoryArc.name)
        .order_by(StoryArc.name)
        .limit(20)
    )
    return [(name, count or 0) for (name, count) in (await session.exec(stmt)).all()]


async def _matching_tags(session: AsyncSession, q: str):
    like = f"%{q}%"
    stmt = (
        select(Tag.name, func.count(func.distinct(ComicTag.comic_id)))
        .join(ComicTag, ComicTag.tag_id == Tag.id, isouter=True)
        .where(Tag.name.ilike(like))
        .group_by(Tag.name)
        .order_by(Tag.name)
        .limit(20)
    )
    return [(name, count or 0) for (name, count) in (await session.exec(stmt)).all()]


async def _attach_copy_counts(session: AsyncSession, items: list[dict]) -> None:
    if not items:
        return
    cids = [it["comic"].id for it in items]
    stmt = (
        select(Copy.comic_id, func.count(Copy.id))
        .where(Copy.comic_id.in_(cids))
        .group_by(Copy.comic_id)
    )
    counts = {cid: n for cid, n in (await session.exec(stmt)).all()}
    for it in items:
        it["copies"] = counts.get(it["comic"].id, 0)


@router.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    session: SessionDep,
    q: str = Query(default=""),
) -> HTMLResponse:
    q = (q or "").strip()
    if not q:
        return templates.TemplateResponse(
            request, "search.html",
            {"q": "", "items": [], "creators": [], "arcs": [], "tags": [], "total": 0},
        )

    items = await _matching_comics(session, q, MAX_RESULTS)
    await _attach_copy_counts(session, items)
    creators = await _matching_creators(session, q)
    arcs = await _matching_arcs(session, q)
    tags = await _matching_tags(session, q)

    return templates.TemplateResponse(
        request, "search.html",
        {
            "q": q, "items": items, "total": len(items),
            "creators": creators, "arcs": arcs, "tags": tags,
            "max_results": MAX_RESULTS,
        },
    )


@router.get("/search/suggest", response_class=HTMLResponse)
async def search_suggest(
    request: Request,
    session: SessionDep,
    q: str = Query(default=""),
) -> HTMLResponse:
    """Inline dropdown for the header search bar — top N comics only,
    no entity-group sections, no copy counts."""
    q = (q or "").strip()
    if not q or len(q) < 2:
        return HTMLResponse("")

    items = await _matching_comics(session, q, SUGGEST_LIMIT)
    return templates.TemplateResponse(
        request, "partials/_search_suggest.html",
        {"q": q, "items": items},
    )
