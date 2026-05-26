"""Factory reset — empty every user-data table.

Schema (alembic_version + table definitions) is left intact so the app
keeps running without restart. Optionally also deletes cover image files
under `covers_dir()`.

This is the most destructive thing the app can do; the route that calls
it is gated by a typed-confirmation phrase. Don't expose this to anyone
without a backup.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from sqlalchemy import delete as sa_delete
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    Character, Comic, ComicArc, ComicCharacter, ComicContainment,
    ComicCreator, ComicSeries, ComicTag, Copy, Creator, ImportRow,
    ImportSession, MetadataCache, Publisher, Series, StoryArc, Tag,
)
from app.services import covers


# Reverse-FK order. Children of join tables first, then their parents.
# Keeping this co-located with the wipe avoids dragging in `portability`
# (which omits the import-wizard tables).
_TABLES_TO_WIPE = [
    # Join + link tables first — anything referencing Comic or Series.
    ComicTag, ComicArc, ComicCharacter, ComicCreator,
    ComicSeries, ComicContainment,
    # Per-row state for the import wizard.
    ImportRow, ImportSession,
    # Then leaf entity rows.
    Copy, Comic,
    # Then parents that the leaves referenced.
    Series, Publisher,
    # Free-floating taxonomy.
    Tag, StoryArc, Character, Creator,
    # External-API cache.
    MetadataCache,
]


@dataclass
class WipeOutcome:
    rows_deleted: int
    cover_files_deleted: int
    cover_dir_kept: bool


async def wipe_everything(
    session: AsyncSession, *, delete_cover_files: bool = True,
) -> WipeOutcome:
    """Truncate every user-data table; optionally delete cover image files.

    The wipe runs inside a single SQL transaction so a failure mid-way
    leaves the original data intact. Cover-file deletion happens AFTER
    the DB commit succeeds — a partial DB+files state is worse than a DB
    that's already clean.
    """
    rows_deleted = 0
    for model in _TABLES_TO_WIPE:
        result = await session.exec(sa_delete(model))
        rows_deleted += int(result.rowcount or 0)
    await session.commit()

    files_deleted = 0
    if delete_cover_files:
        cover_dir = covers.covers_dir()
        if cover_dir.exists():
            for path in cover_dir.iterdir():
                if path.is_file():
                    try:
                        path.unlink()
                        files_deleted += 1
                    except OSError:
                        pass  # best-effort; the DB is already clean

    return WipeOutcome(
        rows_deleted=rows_deleted,
        cover_files_deleted=files_deleted,
        cover_dir_kept=True,
    )
