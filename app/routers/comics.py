from datetime import UTC, date, datetime
from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import SessionLocal, get_session
from app.models import Comic
from app.services import covers

router = APIRouter(prefix="/api/comics", tags=["comics"])

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class ComicCreate(BaseModel):
    series_id: Optional[int] = None
    issue_number: Optional[str] = None
    variant: Optional[str] = None
    title: Optional[str] = None
    cover_date: Optional[date] = None
    page_count: Optional[int] = None
    isbn_10: Optional[str] = None
    isbn_13: Optional[str] = None
    comicvine_id: Optional[str] = None
    metron_id: Optional[str] = None
    marvel_id: Optional[str] = None
    cover_url_local: Optional[str] = None
    cover_url_remote: Optional[str] = None
    description: Optional[str] = None
    cover_price_eur: Optional[float] = None


class ComicUpdate(ComicCreate):
    pass


async def _download_and_store_cover(comic_id: int, remote_url: str) -> None:
    local_url = await covers.download(remote_url)
    if not local_url:
        return
    async with SessionLocal() as session:
        comic = await session.get(Comic, comic_id)
        if comic is None:
            return
        comic.cover_url_local = local_url
        comic.updated_at = datetime.now(UTC)
        session.add(comic)
        await session.commit()


def _maybe_schedule_cover(tasks: BackgroundTasks, comic: Comic) -> None:
    if comic.id is None or not comic.cover_url_remote:
        return
    if comic.cover_url_local:
        return
    tasks.add_task(_download_and_store_cover, comic.id, comic.cover_url_remote)


@router.get("")
async def list_comics(
    session: SessionDep,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[Comic]:
    result = await session.exec(select(Comic).order_by(Comic.id).offset(offset).limit(limit))
    return list(result.all())


@router.get("/{comic_id}")
async def get_comic(comic_id: int, session: SessionDep) -> Comic:
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    return comic


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_comic(
    payload: ComicCreate, session: SessionDep, background: BackgroundTasks
) -> Comic:
    comic = Comic(**payload.model_dump(exclude_unset=True))
    session.add(comic)
    await session.commit()
    await session.refresh(comic)
    _maybe_schedule_cover(background, comic)
    return comic


@router.patch("/{comic_id}")
async def update_comic(
    comic_id: int, payload: ComicUpdate, session: SessionDep, background: BackgroundTasks
) -> Comic:
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    previous_remote = comic.cover_url_remote
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(comic, field, value)
    comic.updated_at = datetime.now(UTC)
    session.add(comic)
    await session.commit()
    await session.refresh(comic)
    if comic.cover_url_remote and comic.cover_url_remote != previous_remote:
        comic.cover_url_local = None
    _maybe_schedule_cover(background, comic)
    return comic


@router.post("/{comic_id}/cover/refresh")
async def refresh_cover(comic_id: int, session: SessionDep) -> Comic:
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    if not comic.cover_url_remote:
        raise HTTPException(status_code=400, detail="comic has no remote cover URL")
    local_url = await covers.download(comic.cover_url_remote)
    if not local_url:
        raise HTTPException(status_code=502, detail="cover download failed")
    comic.cover_url_local = local_url
    comic.updated_at = datetime.now(UTC)
    session.add(comic)
    await session.commit()
    await session.refresh(comic)
    return comic


@router.delete("/{comic_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_comic(comic_id: int, session: SessionDep) -> None:
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    await session.delete(comic)
    await session.commit()
