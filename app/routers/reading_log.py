"""Reading log (`GET /reading-log`).

Renders every Copy with `read_status='read'` and a non-null `date_read`,
newest first, grouped by `YYYY-MM`. The data is identical to what powers
the "Read per month" bar chart on `/stats`, but with full per-comic detail
and cover thumbnails.

Empty state: when no reads are recorded yet, render an explanatory blurb
with a link to the library so users know how to mark something read.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import Comic, Copy, Series

router = APIRouter(tags=["reading-log"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/reading-log", response_class=HTMLResponse)
async def reading_log(request: Request, session: SessionDep) -> HTMLResponse:
    rows = (
        await session.exec(
            select(Copy, Comic, Series)
            .join(Comic, Comic.id == Copy.comic_id)
            .join(Series, Series.id == Comic.series_id, isouter=True)
            .where(Copy.read_status == "read", Copy.date_read.is_not(None))
            .order_by(desc(Copy.date_read), desc(Copy.id))
        )
    ).all()

    # Group by `YYYY-MM`. OrderedDict preserves the desc-by-date sort order.
    buckets: "OrderedDict[str, list[dict]]" = OrderedDict()
    for copy, comic, ser in rows:
        key = copy.date_read.strftime("%Y-%m") if copy.date_read else "unknown"
        buckets.setdefault(key, []).append({
            "copy": copy, "comic": comic, "series": ser,
        })

    # NOTE: do NOT name this key "items" — Jinja's attribute access falls
    # through to dict.items (the builtin) and breaks the loop.
    months = [{"key": k, "entries": v, "count": len(v)} for k, v in buckets.items()]
    return templates.TemplateResponse(
        request, "reading_log.html",
        {"months": months, "total": len(rows)},
    )
