"""Missing-comics pages.

`GET /missing/issues` — canon single issues the library doesn't cover
                        (owned neither as a single nor inside a TPB).
`GET /missing/tpbs`   — canon trade paperbacks not owned.

Both diff the user's library against the canon-comics master index
crawled from Wookieepedia. The first visit kicks the crawl off in the
background; `POST /missing/refresh` rebuilds it on demand.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import Comic
from app.services.canon_index import (
    compute_missing, get_canon_index, get_progress, start_crawl,
)

router = APIRouter(tags=["missing"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/missing")
async def missing_root() -> RedirectResponse:
    return RedirectResponse(url="/missing/issues", status_code=307)


async def _render(request: Request, session: AsyncSession, kind: str) -> HTMLResponse:
    index = await get_canon_index()
    progress = get_progress()

    # First visit with no index and nothing running → kick off the crawl
    # so the page "just works" without a manual build step.
    if index is None and not progress.running:
        await start_crawl()
        progress = get_progress()

    summary = None
    if index is not None:
        comics = list((await session.exec(select(Comic))).all())
        summary = compute_missing(index, comics)[kind]

    return templates.TemplateResponse(
        request,
        "missing.html",
        {
            "kind": kind,
            "summary": summary,
            "index": index,
            "p": progress,
        },
    )


@router.get("/missing/issues", response_class=HTMLResponse)
async def missing_issues(request: Request, session: SessionDep) -> HTMLResponse:
    return await _render(request, session, "issues")


@router.get("/missing/tpbs", response_class=HTMLResponse)
async def missing_tpbs(request: Request, session: SessionDep) -> HTMLResponse:
    return await _render(request, session, "tpbs")


@router.post("/missing/refresh", response_class=HTMLResponse)
async def missing_refresh(request: Request) -> HTMLResponse:
    """Rebuild the canon index. No-op if a crawl is already running."""
    await start_crawl()
    return templates.TemplateResponse(
        request, "partials/_missing_progress.html", {"p": get_progress()},
    )


@router.get("/missing/refresh/status", response_class=HTMLResponse)
async def missing_refresh_status(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/_missing_progress.html", {"p": get_progress()},
    )
