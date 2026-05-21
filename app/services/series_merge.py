"""Series merge — collapse one Series row into another.

Shared by the manual `/series/{id}/merge` tool and the library
cleanup's automatic "subsumed sub-series" consolidation. Kept as a
pure DB helper (no HTTP, no templates) so both callers stay thin.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import delete as sa_delete
from sqlalchemy import text
from sqlalchemy import update as sa_update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import Comic, ComicSeries, Series


async def merge_series(
    session: AsyncSession, source_id: int, target_id: int,
) -> bool:
    """Collapse `source_id` into `target_id`, committing on success.

    Every comic pointing at the source — via the primary `series_id`
    FK or the `ComicSeries` link table — is reassigned to the target,
    then the source row is deleted. Source-derived fields
    (publisher_id, source/source_id, expected_issues) fill blanks on
    the target but never overwrite data it already has.

    Returns False (no-op) when the ids are equal or either row is
    missing — callers can treat that as "nothing to do".
    """
    if source_id == target_id:
        return False
    source = await session.get(Series, source_id)
    target = await session.get(Series, target_id)
    if source is None or target is None:
        return False

    # Adopt source-only metadata onto the target.
    if target.publisher_id is None and source.publisher_id is not None:
        target.publisher_id = source.publisher_id
    if not target.source and source.source:
        target.source = source.source
        target.source_id = source.source_id
    if not target.expected_issues and source.expected_issues:
        target.expected_issues = source.expected_issues

    # Reassign every comic whose primary FK points at the source.
    await session.exec(
        sa_update(Comic)
        .where(Comic.series_id == source_id)
        .values(series_id=target_id, updated_at=datetime.now(UTC))
    )
    # Move multi-series links. INSERT OR IGNORE handles a comic that
    # is already linked to both source and target (PK collision).
    await session.exec(text(
        "INSERT OR IGNORE INTO comicseries "
        "  (comic_id, series_id, is_primary, created_at) "
        "SELECT comic_id, :tgt, is_primary, created_at "
        "FROM comicseries WHERE series_id = :src"
    ).bindparams(tgt=target_id, src=source_id))
    await session.exec(
        sa_delete(ComicSeries).where(ComicSeries.series_id == source_id)
    )
    session.add(target)
    await session.delete(source)
    await session.commit()
    return True
