"""Helpers for the fandom picker + lifespan backfill.

`Comic.fandom` is a free-form lowercase string (e.g. "star wars",
"aggretsuko", "locke & key"). This module centralizes:

  - The dedicated normalization rule (lower + collapsed whitespace).
  - The "list of existing fandoms" query that powers the picker dropdown.
  - The one-shot backfill that runs at startup so comics imported before
    fandom-on-Comic existed pick up `star wars` if they came from
    Wookieepedia.
"""

from __future__ import annotations

import re

from sqlalchemy import update as sa_update
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import SessionLocal
from app.models import Comic


def normalize(value: str | None) -> str | None:
    """Canonical form: lowercase + whitespace-collapsed. Empty / None → None."""
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip().lower()
    return cleaned or None


def display(value: str | None) -> str:
    """Title-case for UI; `None`/empty → empty string."""
    if not value:
        return ""
    return value.title()


async def list_fandoms(session: AsyncSession, *, limit: int = 50) -> list[tuple[str, int]]:
    """Returns [(name, count), ...] sorted by descending comic count.

    Powers the fandom-picker dropdown in `add` / `edit` forms. Capped at
    `limit` so heavily-saturated libraries don't ship 10k options to the
    browser.
    """
    rows = (
        await session.exec(
            select(Comic.fandom, func.count(Comic.id))
            .where(Comic.fandom.is_not(None))
            .group_by(Comic.fandom)
            .order_by(func.count(Comic.id).desc(), Comic.fandom.asc())
            .limit(limit)
        )
    ).all()
    return [(name, int(n)) for (name, n) in rows]


async def backfill_merge_duplicate_series() -> int:
    """Merge Series rows whose normalized names collide into a single row.

    The pre-fix Wookieepedia parser saved series names with embedded
    newlines (multi-value `series=` infobox blobs). Those duplicates
    were created with different raw names, so the dedup probe inside
    `_get_or_create_series` couldn't see they were the same. The
    `backfill_strip_multiline_names` pass cleaned the names — but the
    duplicate ROWS already existed.

    For each group of same-normalized-name series, this picks one
    canonical row (most comics, then lowest id) and reassigns every
    other group member's comics to it. Carries source / source_id /
    expected_issues over from the dupes if the canonical row is empty
    on those fields. Then deletes the dupes. Idempotent.
    """
    from sqlalchemy import update as sa_update, delete as sa_delete
    from app.models import Comic, Series
    from app.routers.add import _normalize_series_name

    async with SessionLocal() as session:
        all_series = (await session.exec(select(Series))).all()
        comic_counts = (await session.exec(
            select(Comic.series_id, func.count(Comic.id))
            .where(Comic.series_id.is_not(None))
            .group_by(Comic.series_id)
        )).all()
        count_by_id = {sid: int(n) for sid, n in comic_counts}

        # Group by normalized name.
        groups: dict[str, list[Series]] = {}
        for s in all_series:
            if not s.name:
                continue
            key = _normalize_series_name(s.name)
            groups.setdefault(key, []).append(s)

        merged = 0
        for key, members in groups.items():
            if len(members) < 2:
                continue
            # Canonical: most comics, then lowest id.
            members.sort(key=lambda s: (-count_by_id.get(s.id, 0), s.id))
            canonical = members[0]
            losers = members[1:]

            # Carry over missing source / source_id / expected_issues.
            for loser in losers:
                if not canonical.source and loser.source:
                    canonical.source = loser.source
                if not canonical.source_id and loser.source_id:
                    canonical.source_id = loser.source_id
                if not canonical.expected_issues and loser.expected_issues:
                    canonical.expected_issues = loser.expected_issues
                if canonical.publisher_id is None and loser.publisher_id is not None:
                    canonical.publisher_id = loser.publisher_id
            session.add(canonical)

            # Reassign every comic + delete the loser rows.
            loser_ids = [l.id for l in losers]
            await session.exec(
                sa_update(Comic)
                .where(Comic.series_id.in_(loser_ids))
                .values(series_id=canonical.id)
            )
            await session.exec(
                sa_delete(Series).where(Series.id.in_(loser_ids))
            )
            merged += len(losers)

        if merged:
            await session.commit()
        return merged


async def backfill_strip_multiline_names() -> int:
    """One-shot cleanup for `Series.name`, `Comic.title`, `Publisher.name`
    values that accidentally got persisted with embedded newlines (e.g.
    Wookieepedia ComicBook articles whose `series=` field carried a
    multi-value blob). Keeps only the first non-empty line.

    Idempotent — safe to call on every cold start.
    """
    from app.models import Comic, Publisher, Series
    n = 0
    async with SessionLocal() as session:
        for model in (Series, Comic, Publisher):
            attr = "name" if model is not Comic else "title"
            rows = (await session.exec(select(model))).all()
            for row in rows:
                cur = getattr(row, attr)
                if not cur or "\n" not in cur:
                    continue
                first = next((ln.strip() for ln in cur.splitlines() if ln.strip()), None)
                if first and first != cur:
                    setattr(row, attr, first)
                    session.add(row)
                    n += 1
        if n:
            await session.commit()
    return n


async def backfill_normalize_format() -> int:
    """One-shot lower-case sweep over `Comic.format`. Older saves wrote
    whatever casing the source returned ("Trade Paperback", "TPB") which
    fragmented the library filter facets. This rewrites every non-null
    value to the canonical lowercase form so the chips collapse cleanly.

    Idempotent — safe to call on every cold start.
    """
    from app.models import Comic
    from app.services.csv_import import translate_format
    async with SessionLocal() as session:
        rows = (await session.exec(
            select(Comic).where(Comic.format.is_not(None))
        )).all()
        n = 0
        for c in rows:
            new = translate_format(c.format)
            if new != c.format:
                c.format = new
                session.add(c)
                n += 1
        if n:
            await session.commit()
        return n


async def backfill_wookieepedia_fandom() -> int:
    """Set `comic.fandom = 'star wars'` for any comic added via Wookieepedia
    that doesn't already have a fandom. Idempotent — safe to call on every
    cold start. Returns the number of rows updated.
    """
    async with SessionLocal() as session:
        result = await session.exec(
            sa_update(Comic)
            .where(Comic.source == "wookieepedia", Comic.fandom.is_(None))
            .values(fandom="star wars")
        )
        await session.commit()
        return int(result.rowcount or 0)


async def backfill_comic_series_links() -> int:
    """Mirror every Comic.series_id value into the ComicSeries link
    table so multi-series-aware views see the primary series too.
    Idempotent — uses INSERT OR IGNORE on the (comic_id, series_id)
    composite primary key. Returns rows added.

    Necessary because:
      1. Comics saved BEFORE migration 0009 may still lack a
         ComicSeries row if the migration's one-shot backfill couldn't
         reach them (e.g. in long-lived dev DBs that skipped the
         migration's INSERT pass).
      2. Save paths added before the multi-series schema landed
         (CSV import, /add/save, etc.) write `Comic.series_id` only;
         this catches them up on the next cold start.
    """
    from sqlalchemy import text
    async with SessionLocal() as session:
        result = await session.exec(text(
            "INSERT OR IGNORE INTO comicseries "
            "  (comic_id, series_id, is_primary, created_at) "
            "SELECT id, series_id, 1, CURRENT_TIMESTAMP "
            "FROM comic WHERE series_id IS NOT NULL"
        ))
        await session.commit()
        return int(result.rowcount or 0)
