"""Tag management routes for the comic-detail page.

POST /comic/{id}/tags        → add a tag (find-or-create), returns tags partial.
POST /comic/{id}/tags/remove → unlink a tag from this comic, returns tags partial.

The detail page mounts a `_tags.html` partial under `#tags-section` so HTMX
swaps just that block on each change.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from urllib.parse import quote

from app.db import get_session
from app.models import Comic, ComicTag, Tag

router = APIRouter(tags=["tags"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _normalize_tag(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().lower()


async def _tags_for(session: AsyncSession, comic_id: int) -> list[Tag]:
    result = await session.exec(
        select(Tag)
        .join(ComicTag, ComicTag.tag_id == Tag.id)
        .where(ComicTag.comic_id == comic_id)
        .order_by(Tag.name)
    )
    return list(result.all())


async def _render_tags(
    request: Request, session: AsyncSession, comic_id: int,
    *, flash: str | None = None,
) -> HTMLResponse:
    tags = await _tags_for(session, comic_id)
    return templates.TemplateResponse(
        request, "partials/_tags.html",
        {"comic_id": comic_id, "tags": tags, "flash": flash},
    )


@router.get("/tags", response_class=HTMLResponse)
async def tags_index(request: Request, session: SessionDep) -> HTMLResponse:
    """All-tags index. Each entry links to the matching filtered library view.

    The library router already accepts `?tag=<name>` as a filter — this page
    is just a discoverable, sorted entry point so users can jump straight to
    a tag they care about without going through a comic detail first.
    """
    rows = (
        await session.exec(
            select(Tag.name, func.count(ComicTag.comic_id).label("n"))
            .join(ComicTag, ComicTag.tag_id == Tag.id, isouter=True)
            .group_by(Tag.id, Tag.name)
            .order_by(func.count(ComicTag.comic_id).desc(), Tag.name.asc())
        )
    ).all()
    tags = [{"name": name, "count": int(n or 0)} for (name, n) in rows]
    return templates.TemplateResponse(
        request, "tags.html", {"tags": tags},
    )


@router.get("/tag/{name}")
async def tag_redirect(name: str) -> RedirectResponse:
    """Convenience redirect: /tag/foo → /library?tag=foo."""
    return RedirectResponse(url=f"/library?tag={quote(name, safe='')}", status_code=302)


@router.post("/comic/{comic_id}/tags", response_class=HTMLResponse)
async def add_tag(
    comic_id: int,
    request: Request,
    session: SessionDep,
    name: str = Form(...),
) -> HTMLResponse:
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    name = _normalize_tag(name)
    if not name:
        return await _render_tags(request, session, comic_id)

    result = await session.exec(select(Tag).where(Tag.name == name))
    tag = result.first()
    if tag is None:
        tag = Tag(name=name)
        session.add(tag)
        await session.flush()

    link_result = await session.exec(
        select(ComicTag).where(ComicTag.comic_id == comic_id, ComicTag.tag_id == tag.id)
    )
    if link_result.first() is None:
        session.add(ComicTag(comic_id=comic_id, tag_id=tag.id))
        await session.commit()

    return await _render_tags(request, session, comic_id)


@router.post("/comic/{comic_id}/auto-tag", response_class=HTMLResponse)
async def auto_tag(
    comic_id: int, request: Request, session: SessionDep,
) -> HTMLResponse:
    """Backfill tags from the comic's original source. Re-fetches the
    candidate (cached if already pulled within the cache TTL) and applies
    `_autotag_from_candidate`. No-op when the comic has no `source` /
    `source_id` set.

    Existing tags are preserved — only adds.
    """
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    flash: str
    if not (comic.source and comic.source_id):
        flash = "No source linked — auto-tag needs the comic's original source. Try the refresh button to link one."
    else:
        from app.routers.add import _refetch_candidate
        from app.routers.detail import _autotag_from_candidate
        candidate = await _refetch_candidate(comic.source, comic.source_id)
        if candidate is None:
            flash = f"Couldn't reach {comic.source} for this comic — try again later."
        else:
            n = await _autotag_from_candidate(session, comic_id, candidate)
            await session.commit()
            if n == 0:
                flash = f"{comic.source} returned no characters or arcs to tag."
            else:
                flash = f"Added {n} tag{'' if n == 1 else 's'} from {comic.source}."
    return await _render_tags(request, session, comic_id, flash=flash)


@router.post("/comic/{comic_id}/tags/remove", response_class=HTMLResponse)
async def remove_tag(
    comic_id: int,
    request: Request,
    session: SessionDep,
    tag_id: int = Form(...),
) -> HTMLResponse:
    link_result = await session.exec(
        select(ComicTag).where(ComicTag.comic_id == comic_id, ComicTag.tag_id == tag_id)
    )
    link = link_result.first()
    if link is not None:
        await session.delete(link)
        await session.commit()
    return await _render_tags(request, session, comic_id)
