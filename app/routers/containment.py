"""Comic-containment endpoints (omnibus → TPB and similar links).

Three routes:

  GET  /comic/{id}/contains/search?q=...   — typeahead for the "Add"
                                              form. Returns HTML
                                              fragment with library
                                              matches first, then a
                                              "Search Wookieepedia"
                                              row for free-text
                                              upstream lookup.
  POST /comic/{id}/contains                — add a child link. Accepts
                                              either `child_id` (an
                                              existing Comic) or
                                              `wookieepedia_title`
                                              (creates a stub Comic
                                              first, then links).
  DELETE /comic/{id}/contains/{child_id}   — remove a link.

Stub Comics have no Copy rows attached and are filtered out of the
default /library view; see `library.py` for the `tracked` filter
chip that reveals them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete as sa_delete, func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import Comic, ComicContainment, Copy, Publisher, Series
from app.services import wookieepedia

router = APIRouter(tags=["containment"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ── Search (typeahead) ────────────────────────────────────────────────


@router.get("/comic/{comic_id}/contains/search", response_class=HTMLResponse)
async def contains_search(
    comic_id: int, request: Request, session: SessionDep, q: str = "",
) -> HTMLResponse:
    """Typeahead results for the Add-child form. Returns up to 8
    library matches first (excluding the parent itself and anything
    already linked), then a Wookieepedia-lookup option."""
    q = (q or "").strip()
    if not q:
        return HTMLResponse("")

    # Skip self + already-linked children to keep the list relevant.
    existing_links = (await session.exec(
        select(ComicContainment.child_id)
        .where(ComicContainment.parent_id == comic_id)
    )).all()
    blocked = {comic_id} | {
        (r if isinstance(r, int) else r[0]) for r in existing_links
    }

    pattern = f"%{q.lower()}%"
    matches = (await session.exec(
        select(Comic)
        .where(func.lower(Comic.title).like(pattern))
        .where(~Comic.id.in_(blocked))
        .order_by(Comic.title)
        .limit(8)
    )).all()

    return templates.TemplateResponse(
        request,
        "partials/_contains_search.html",
        {"comic_id": comic_id, "q": q, "matches": matches},
    )


# ── Add link ──────────────────────────────────────────────────────────


async def _create_stub_from_wookieepedia(
    session: AsyncSession, article_title: str,
) -> Optional[Comic]:
    """Fetch a Wookieepedia article and create a stub Comic row (no
    Copy attached) carrying its metadata. Used so the user can link an
    Omnibus to TPBs they don't yet own. Returns the stub, or None if
    the upstream article doesn't exist."""
    cand = await wookieepedia.get_article(article_title)
    if cand is None:
        return None

    publisher_row = None
    if cand.publisher:
        result = await session.exec(
            select(Publisher).where(Publisher.name == cand.publisher)
        )
        publisher_row = result.first()
        if publisher_row is None:
            slug = (cand.publisher or "").lower().replace(" ", "-") or "unknown"
            publisher_row = Publisher(name=cand.publisher, slug=slug)
            session.add(publisher_row)
            await session.flush()

    series_row = None
    if cand.series:
        result = await session.exec(
            select(Series).where(Series.name == cand.series)
        )
        series_row = result.first()
        if series_row is None:
            series_row = Series(
                name=cand.series,
                publisher_id=publisher_row.id if publisher_row else None,
            )
            session.add(series_row)
            await session.flush()

    from app.routers.add import _parse_date  # late import to avoid cycle
    stub = Comic(
        series_id=series_row.id if series_row else None,
        title=cand.title,
        issue_number=cand.issue_number,
        cover_date=_parse_date(cand.cover_date),
        page_count=cand.page_count,
        isbn_10=cand.isbn_10,
        isbn_13=cand.isbn_13,
        upc=cand.upc,
        cover_url_remote=cand.cover_url,
        description=cand.description,
        source="wookieepedia",
        source_id=article_title,
        collected_issues=cand.collected_issues,
        format=cand.format,
        language=cand.language,
        timeline=cand.timeline,
        era=cand.era,
        canon=cand.canon,
        fandom="star wars",
    )
    session.add(stub)
    await session.commit()
    await session.refresh(stub)
    return stub


@router.post("/comic/{comic_id}/contains", response_class=HTMLResponse)
async def contains_add(
    comic_id: int,
    request: Request,
    session: SessionDep,
    background: BackgroundTasks,
    child_id: str = Form(default=""),
    wookieepedia_title: str = Form(default=""),
) -> HTMLResponse:
    """Add a child link to the parent comic. Provide either
    `child_id` (existing Comic) or `wookieepedia_title` (creates a
    stub Comic via Wookieepedia lookup, then links it).

    Returns the refreshed Contains section partial for HTMX swap.
    """
    parent = await session.get(Comic, comic_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="parent comic not found")

    child: Optional[Comic] = None
    if child_id.isdigit():
        child = await session.get(Comic, int(child_id))
    elif wookieepedia_title.strip():
        child = await _create_stub_from_wookieepedia(
            session, wookieepedia_title.strip(),
        )

    if child is None:
        raise HTTPException(
            status_code=422,
            detail="Need a valid child_id or wookieepedia_title.",
        )
    if child.id == parent.id:
        raise HTTPException(
            status_code=422,
            detail="A comic can't contain itself.",
        )

    # Already linked? Idempotent — just return the current state.
    existing = (await session.exec(
        select(ComicContainment).where(
            ComicContainment.parent_id == parent.id,
            ComicContainment.child_id == child.id,
        )
    )).first()
    if existing is None:
        # Position = highest+1 to preserve insertion order without
        # forcing the user to drag-reorder.
        max_pos = (await session.exec(
            select(func.max(ComicContainment.position))
            .where(ComicContainment.parent_id == parent.id)
        )).first()
        next_pos = (max_pos[0] if isinstance(max_pos, tuple) else max_pos) or 0
        link = ComicContainment(
            parent_id=parent.id, child_id=child.id, position=int(next_pos) + 1,
        )
        session.add(link)
        await session.commit()

    # For new stub comics with a remote cover, schedule the background
    # download so the cover appears in the Contains section without a
    # manual refresh.
    if child.cover_url_remote and not child.cover_url_local:
        from app.routers.add import _download_and_store_cover
        background.add_task(
            _download_and_store_cover, child.id, child.cover_url_remote,
        )

    return await _render_contains_partial(request, session, parent.id)


# ── Remove link ───────────────────────────────────────────────────────


@router.post(
    "/comic/{comic_id}/contains/{child_id}/delete",
    response_class=HTMLResponse,
)
async def contains_remove(
    comic_id: int, child_id: int, request: Request, session: SessionDep,
) -> HTMLResponse:
    """Remove a child link. Doesn't delete the child Comic itself —
    stub Comics persist independently and can be re-linked elsewhere.
    """
    await session.exec(
        sa_delete(ComicContainment).where(
            ComicContainment.parent_id == comic_id,
            ComicContainment.child_id == child_id,
        )
    )
    await session.commit()
    return await _render_contains_partial(request, session, comic_id)


# ── Render helper ─────────────────────────────────────────────────────


async def _render_contains_partial(
    request: Request, session: AsyncSession, comic_id: int,
) -> HTMLResponse:
    """Re-render the Contains section partial after add / remove."""
    rows = (await session.exec(
        select(Comic, ComicContainment)
        .join(ComicContainment, ComicContainment.child_id == Comic.id)
        .where(ComicContainment.parent_id == comic_id)
        .order_by(ComicContainment.position.asc(), Comic.id.asc())
    )).all()

    # Tag each child with owned vs tracked so the template can badge
    # stubs differently.
    children: list[dict] = []
    for child, _link in rows:
        n_copies = (await session.exec(
            select(func.count(Copy.id)).where(Copy.comic_id == child.id)
        )).first()
        copies = n_copies[0] if isinstance(n_copies, tuple) else (n_copies or 0)
        children.append({"comic": child, "owned": int(copies or 0) > 0})

    return templates.TemplateResponse(
        request,
        "partials/_contains.html",
        {"parent_id": comic_id, "children": children},
    )
