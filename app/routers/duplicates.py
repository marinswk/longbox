"""Duplicates view — comics where you own more than one copy.

GET /duplicates  →  ranked grid, highest copy-count first.

Pure aggregation, no schema. Useful for trade/sale planning ("which
comics do I have multiples of?") and for spotting accidental
double-saves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import Comic, Copy, Publisher, Series

router = APIRouter(tags=["duplicates"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/duplicates", response_class=HTMLResponse)
async def duplicates_page(request: Request, session: SessionDep) -> HTMLResponse:
    # Sub-aggregate: comic_id -> count of copies, only keeping count > 1.
    copy_count = func.count(Copy.id).label("copy_count")
    counts_sub = (
        select(Copy.comic_id, copy_count)
        .group_by(Copy.comic_id)
        .having(copy_count > 1)
        .subquery()
    )

    stmt = (
        select(Comic, Series, Publisher, counts_sub.c.copy_count)
        .select_from(counts_sub)
        .join(Comic, Comic.id == counts_sub.c.comic_id)
        .join(Series, Series.id == Comic.series_id, isouter=True)
        .join(Publisher, Publisher.id == Series.publisher_id, isouter=True)
        .order_by(desc(counts_sub.c.copy_count), Comic.title)
    )
    rows = (await session.exec(stmt)).all()

    items = [
        {"comic": comic, "series": ser, "publisher": pub, "copies": int(n)}
        for (comic, ser, pub, n) in rows
    ]
    total_dup_comics = len(items)
    total_extra_copies = sum(it["copies"] - 1 for it in items)

    return templates.TemplateResponse(
        request,
        "duplicates.html",
        {
            "items": items,
            "total_dup_comics": total_dup_comics,
            "total_extra_copies": total_extra_copies,
        },
    )
