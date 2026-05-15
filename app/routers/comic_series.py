"""Multi-series management for a Comic.

  GET    /comic/{id}/series/search?q=...    typeahead for the
                                              "Add to series" form.
  POST   /comic/{id}/series                  attach to a series
                                              (existing or new by name).
  POST   /comic/{id}/series/{sid}/delete     remove a non-primary link.

The Comic's `series_id` FK is the "primary" series and stays fixed
unless the user explicitly chooses to rename. We never let the user
remove the primary link from this form — that's the merge UI's job.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete as sa_delete, func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import Comic, ComicSeries, Series

router = APIRouter(tags=["comic-series"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/comic/{comic_id}/series/search", response_class=HTMLResponse)
async def comic_series_search(
    comic_id: int, request: Request, session: SessionDep, q: str = "",
) -> HTMLResponse:
    """Typeahead for the multi-series-attach form. Returns up to 8
    Series matches, excluding ones already linked to the comic.
    Always offers a "Create new series '<q>'" option so the user can
    introduce a series that doesn't exist yet."""
    q = (q or "").strip()
    if not q:
        return HTMLResponse("")

    # Already-linked series (via either the primary FK or the link
    # table) are filtered out.
    already_linked = set()
    comic = await session.get(Comic, comic_id)
    if comic and comic.series_id is not None:
        already_linked.add(comic.series_id)
    link_rows = (await session.exec(
        select(ComicSeries.series_id)
        .where(ComicSeries.comic_id == comic_id)
    )).all()
    for r in link_rows:
        already_linked.add(r if isinstance(r, int) else r[0])

    pattern = f"%{q.lower()}%"
    matches = (await session.exec(
        select(Series)
        .where(func.lower(Series.name).like(pattern))
        .where(~Series.id.in_(already_linked))
        .order_by(Series.name)
        .limit(8)
    )).all()

    return templates.TemplateResponse(
        request,
        "partials/_comic_series_search.html",
        {"comic_id": comic_id, "q": q, "matches": matches},
    )


async def _render_series_partial(
    request: Request, session: AsyncSession, comic_id: int,
) -> HTMLResponse:
    """Re-render the Series section of the comic detail page."""
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")

    # Gather series linked via either the primary FK or the link
    # table, deduped. The primary one is always rendered first +
    # marked so the UI doesn't offer to remove it.
    series_rows: list[tuple[Series, bool]] = []
    seen: set[int] = set()
    if comic.series_id is not None:
        primary = await session.get(Series, comic.series_id)
        if primary is not None:
            series_rows.append((primary, True))
            seen.add(primary.id)
    link_rows = (await session.exec(
        select(Series, ComicSeries.is_primary)
        .join(ComicSeries, ComicSeries.series_id == Series.id)
        .where(ComicSeries.comic_id == comic_id)
        .order_by(Series.name)
    )).all()
    for s, is_primary in link_rows:
        if s.id in seen:
            continue
        seen.add(s.id)
        series_rows.append((s, bool(is_primary)))

    return templates.TemplateResponse(
        request,
        "partials/_comic_series.html",
        {"comic_id": comic_id, "series_rows": series_rows},
    )


@router.post("/comic/{comic_id}/series", response_class=HTMLResponse)
async def comic_series_add(
    comic_id: int, request: Request, session: SessionDep,
    series_id: str = Form(default=""),
    new_series_name: str = Form(default=""),
) -> HTMLResponse:
    """Attach the comic to a series. Provide either `series_id` (an
    existing Series) or `new_series_name` (creates the row first).
    Inherits publisher_id from the comic's existing primary series
    when creating a new one — best guess; the user can correct it
    from the series detail page later."""
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")

    target: Series | None = None
    if series_id.isdigit():
        target = await session.get(Series, int(series_id))
    elif new_series_name.strip():
        # Find-or-create by name. The publisher_id inheritance keeps
        # the new series compatible with the library's publisher
        # facets right away.
        inherited_pub = None
        if comic.series_id is not None:
            primary_series = await session.get(Series, comic.series_id)
            if primary_series is not None:
                inherited_pub = primary_series.publisher_id
        existing = (await session.exec(
            select(Series).where(Series.name == new_series_name.strip())
        )).first()
        if existing is not None:
            target = existing
        else:
            target = Series(
                name=new_series_name.strip(), publisher_id=inherited_pub,
            )
            session.add(target)
            await session.flush()

    if target is None:
        raise HTTPException(
            status_code=422,
            detail="Need a valid series_id or new_series_name.",
        )

    # Idempotent: do nothing if already linked (FK or link table).
    if comic.series_id == target.id:
        return await _render_series_partial(request, session, comic_id)
    existing_link = (await session.exec(
        select(ComicSeries).where(
            ComicSeries.comic_id == comic_id,
            ComicSeries.series_id == target.id,
        )
    )).first()
    if existing_link is None:
        session.add(ComicSeries(
            comic_id=comic_id, series_id=target.id, is_primary=False,
        ))
        await session.commit()

    return await _render_series_partial(request, session, comic_id)


@router.post(
    "/comic/{comic_id}/series/{series_id}/delete",
    response_class=HTMLResponse,
)
async def comic_series_remove(
    comic_id: int, series_id: int, request: Request, session: SessionDep,
) -> HTMLResponse:
    """Remove a non-primary series link. The primary link can't be
    removed here — use the series merge UI for that — to avoid
    accidentally orphaning the comic from its main series."""
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    if comic.series_id == series_id:
        raise HTTPException(
            status_code=422,
            detail="Can't remove the primary series here. Use the "
                   "series merge UI to change a comic's primary series.",
        )
    await session.exec(
        sa_delete(ComicSeries).where(
            ComicSeries.comic_id == comic_id,
            ComicSeries.series_id == series_id,
        )
    )
    await session.commit()
    return await _render_series_partial(request, session, comic_id)
