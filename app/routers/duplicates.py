"""Duplicates view — issue-level duplicate detection across single
issues, TPBs, and omnibuses.

GET /duplicates  →  reverse-index of `issue article title → owning
                    Comics`, filtered to entries with ≥2 owners.

The same underlying issue can appear via:
  * a single-issue Comic whose `source_id` is that article;
  * a TPB / omnibus whose `collected_issues` lists that article.

Computation lives in `app.services.duplicates`; this router is just
glue (filter / sort / group params → render).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import Comic, Copy, Series
from app.services.duplicates import (
    apply_filters_and_sort, build_duplicate_index, group_by_series, stats,
)

router = APIRouter(tags=["duplicates"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]

_MIX_VALUES = {"all", "singles_and_collection", "collections_only"}
_SORT_VALUES = {"count_desc", "title_asc", "series_asc"}


@router.get("/duplicates", response_class=HTMLResponse)
async def duplicates_page(
    request: Request,
    session: SessionDep,
    mix: str = Query(default="all"),
    sort: str = Query(default="count_desc"),
    series: str = Query(default=""),
    group: str = Query(default="series"),
    min_copies: int = Query(default=2, ge=2, le=10),
) -> HTMLResponse:
    if mix not in _MIX_VALUES:
        mix = "all"
    if sort not in _SORT_VALUES:
        sort = "count_desc"
    group = "series" if group != "none" else "none"

    # Owned comics only: Copy count > 0. Stubs (no Copy attached)
    # don't count toward duplicate detection — they wouldn't make
    # physical sense.
    owned_ids = (await session.exec(
        select(Copy.comic_id).distinct()
    )).all()
    owned_ids = {r if isinstance(r, int) else r[0] for r in owned_ids}
    if not owned_ids:
        owned_comics: list[Comic] = []
    else:
        owned_comics = (await session.exec(
            select(Comic).where(Comic.id.in_(owned_ids))
        )).all()

    all_series = (await session.exec(select(Series))).all()

    rows = build_duplicate_index(
        owned_comics, all_series, min_copies=min_copies,
    )
    rows = apply_filters_and_sort(
        rows,
        mix=mix,
        sort=sort,
        series=series or None,
    )
    summary = stats(rows)

    groups = group_by_series(rows) if group == "series" else [
        {"label": None, "rows": rows, "count": sum(r.count - 1 for r in rows)}
    ]

    # Series filter facet: every series name that has at least one
    # duplicate row, with the duplicate count.
    series_facet_counts: dict[str, int] = {}
    for r in rows:
        series_facet_counts[r.derived_series] = (
            series_facet_counts.get(r.derived_series, 0) + 1
        )
    series_facet = sorted(
        series_facet_counts.items(), key=lambda kv: (-kv[1], kv[0].lower()),
    )

    return templates.TemplateResponse(
        request,
        "duplicates.html",
        {
            "summary": summary,
            "groups": groups,
            "series_facet": series_facet,
            "selected": {
                "mix": mix,
                "sort": sort,
                "series": series,
                "group": group,
                "min_copies": min_copies,
            },
        },
    )
