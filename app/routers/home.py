"""Landing page (`GET /`).

Renders different content based on whether the library is empty:

* **Empty:** dark hero + onboarding strip explaining the add flow.
* **Loaded:** hero with live counts, a recent-additions cover strip, and
  series-progress highlights ranked by % owned.

Both states render from the same `index.html` template — the differences
are pure data switches (`is_empty`, populated lists).
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
from app.models import Comic, ComicCreator, Creator, Publisher, Series
from app.services.series_progress import compute_progress

router = APIRouter(tags=["home"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]

RECENT_LIMIT = 6
PROGRESS_LIMIT = 5
TOP_CREATORS_LIMIT = 8


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, session: SessionDep) -> HTMLResponse:
    total_comics = (await session.exec(select(func.count(Comic.id)))).one()
    if isinstance(total_comics, tuple):
        total_comics = total_comics[0]
    total_comics = int(total_comics or 0)

    if total_comics == 0:
        return templates.TemplateResponse(
            request, "index.html",
            {"is_empty": True, "totals": {"comics": 0, "series": 0, "publishers": 0},
             "recent": [], "progress_rows": []},
        )

    total_series = int((await session.exec(
        select(func.count(func.distinct(Comic.series_id)))
        .where(Comic.series_id.is_not(None))
    )).one() or 0)
    total_publishers = int((await session.exec(
        select(func.count(func.distinct(Series.publisher_id)))
        .where(Series.publisher_id.is_not(None))
    )).one() or 0)

    # Recent additions — N most recently created comics with their series
    # so we can show a publisher hint under each cover.
    recent_rows = (
        await session.exec(
            select(Comic, Series, Publisher)
            .select_from(Comic)
            .join(Series, Series.id == Comic.series_id, isouter=True)
            .join(Publisher, Publisher.id == Series.publisher_id, isouter=True)
            .order_by(desc(Comic.created_at))
            .limit(RECENT_LIMIT)
        )
    ).all()
    recent = [
        {"comic": comic, "series": ser, "publisher": pub}
        for (comic, ser, pub) in recent_rows
    ]

    # Series-progress highlights: only series with a refreshed issue list,
    # ranked by % complete (highest first), with a tie-break on owned-count
    # so a 100%-of-3 series doesn't outrank a 95%-of-100 series visually.
    series_with_lists = (
        await session.exec(
            select(Series).where(Series.expected_issues.is_not(None))
        )
    ).all()
    progress = await compute_progress(session, [s.id for s in series_with_lists])
    progress_rows = []
    for s in series_with_lists:
        p = progress.get(s.id)
        if p is None or p.total == 0:
            continue
        progress_rows.append({"series": s, "progress": p})
    progress_rows.sort(key=lambda r: (-r["progress"].pct, -r["progress"].total))
    progress_rows = progress_rows[:PROGRESS_LIMIT]

    # Top-N creators by distinct-comics-credited. Roles are ignored — a
    # writer who's also drawn an issue is still one credit.
    from sqlalchemy import desc as _desc, func as _func
    creator_rows = (
        await session.exec(
            select(
                Creator.name,
                _func.count(_func.distinct(ComicCreator.comic_id)).label("n"),
            )
            .join(ComicCreator, ComicCreator.creator_id == Creator.id)
            .group_by(Creator.id, Creator.name)
            .order_by(_desc("n"))
            .limit(TOP_CREATORS_LIMIT)
        )
    ).all()
    top_creators = [{"name": name, "count": int(n)} for (name, n) in creator_rows]

    return templates.TemplateResponse(
        request, "index.html",
        {
            "is_empty": False,
            "totals": {
                "comics": total_comics,
                "series": total_series,
                "publishers": total_publishers,
            },
            "recent": recent,
            "progress_rows": progress_rows,
            "top_creators": top_creators,
        },
    )
