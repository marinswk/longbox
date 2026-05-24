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


async def backfill_prune_empty_inferred_series() -> int:
    """Drop Series rows that look like a stale inference artefact:
    no expected_issues, not the primary FK of any comic, and only
    multi-series link-table references (or none at all).

    Created by `_attach_inferred_series` before we added the "skip
    when issues=[]" guard. Manifestation: rows like "Star Wars: The
    Old Republic, Blood of the Empire" or "Star Wars: The Old
    Republic, Threat of Peace" that appeared on /comic/{id}'s
    SERIES section as 0/0 chips with no real value.

    Idempotent. Runs after the inferrer backfill so any rows the
    inferrer just touched and successfully populated stay.

    Returns the number of series rows removed.
    """
    from sqlalchemy import text
    async with SessionLocal() as session:
        # The query: a Series is "empty-inferred" iff
        #   (expected_issues IS NULL OR = '') AND
        #   no Comic has series_id = this.id AND
        #   no comic primary-FK references it.
        # We KEEP rows with expected_issues set (those are real
        # tracking targets, even if they have no owned comics yet —
        # the user may have added them via manual link).
        # We also KEEP rows that are SOMEONE's primary series.
        rows = await session.exec(text(
            "DELETE FROM comicseries WHERE series_id IN ("
            "  SELECT s.id FROM series s "
            "  WHERE (s.expected_issues IS NULL OR s.expected_issues = '') "
            "    AND NOT EXISTS (SELECT 1 FROM comic c WHERE c.series_id = s.id)"
            ")"
        ))
        link_n = int(rows.rowcount or 0)
        result = await session.exec(text(
            "DELETE FROM series WHERE id IN ("
            "  SELECT s.id FROM series s "
            "  WHERE (s.expected_issues IS NULL OR s.expected_issues = '') "
            "    AND NOT EXISTS (SELECT 1 FROM comic c WHERE c.series_id = s.id)"
            ")"
        ))
        await session.commit()
        return int(result.rowcount or 0)


async def backfill_inferred_series_from_collected_issues() -> int:
    """For every Comic with a non-empty `collected_issues` blob, derive
    the implied series names from each `<Series> <Issue Number>` entry
    and attach the comic to every matching series via ComicSeries.
    Idempotent — skips comics already linked to a given series.

    Creates new Series rows for derived names that don't exist yet,
    inheriting publisher_id from the comic's primary series. Returns
    the total number of new link rows written across all comics.

    Runs on every cold start so legacy data (omnibuses / TPBs saved
    before the multi-series schema existed) auto-populates without
    forcing the user to hit a refresh button on every comic.
    """
    # Late import to avoid the routers→services→routers cycle.
    from app.routers.add import _attach_inferred_series

    total = 0
    async with SessionLocal() as session:
        rows = (await session.exec(
            select(Comic.id)
            .where(Comic.collected_issues.is_not(None))
            .where(Comic.collected_issues != "")
        )).all()
    comic_ids = [r if isinstance(r, int) else r[0] for r in rows]
    # Run per-comic in its own session — `_attach_inferred_series`
    # opens one internally — so a single misbehaving entry doesn't
    # roll back the whole backfill batch.
    for cid in comic_ids:
        try:
            # Count link rows before + after to know what we added.
            from app.models import ComicSeries
            async with SessionLocal() as session:
                before_rows = (await session.exec(
                    select(ComicSeries.series_id)
                    .where(ComicSeries.comic_id == cid)
                )).all()
                before = len({r if isinstance(r, int) else r[0] for r in before_rows})
            await _attach_inferred_series(cid)
            async with SessionLocal() as session:
                after_rows = (await session.exec(
                    select(ComicSeries.series_id)
                    .where(ComicSeries.comic_id == cid)
                )).all()
                after = len({r if isinstance(r, int) else r[0] for r in after_rows})
            total += max(0, after - before)
        except Exception:
            # Best-effort; don't let one weird entry break startup.
            continue
    return total


async def backfill_single_issue_format() -> int:
    """Set `format='single issue'` on every wookieepedia-sourced Comic
    whose format is NULL AND that lacks the two trade markers (an
    ISBN-13 and a non-empty collected_issues blob). Idempotent —
    no-op once every legacy row has a format.

    Single issues on Wookieepedia use `{{ComicBook}}` which lacks a
    `media type=` field, so before the per-template default landed,
    every imported single comic ended up with `format=NULL`. The
    "no ISBN AND no contents list" filter avoids touching trades
    that legitimately have no `media type` set upstream (extremely
    rare, but cheap to be safe).
    """
    from sqlalchemy import text
    async with SessionLocal() as session:
        result = await session.exec(text(
            "UPDATE comic SET format = 'single issue' "
            "WHERE source = 'wookieepedia' "
            "  AND format IS NULL "
            "  AND (isbn_13 IS NULL OR isbn_13 = '') "
            "  AND (collected_issues IS NULL OR collected_issues = '')"
        ))
        await session.commit()
        return int(result.rowcount or 0)


async def backfill_strip_bogus_movie_adaptation_links() -> int:
    """Drop ComicSeries links to a series named `Star Wars Movie
    Adaptations` from comics whose own title doesn't look like a movie
    adaptation. Idempotent — no-op once the DB is clean.

    Earlier the Wookieepedia film-adaptation fallback fired on any
    article carrying the `Comic film adaptations` category, including
    tie-in / promo one-shots (e.g. `Episode I: The Phantom Menace ½`).
    When such a one-shot was collected inside an Epic Collection /
    omnibus, `backfill_inferred_series_from_collected_issues` dragged
    the containing volume into the umbrella series. The fallback is
    now title-gated, but the bogus link rows it produced still need a
    one-time sweep.

    A comic is a true movie adaptation iff its title contains
    'Adaptation' or 'Graphic Novel' (case-insensitive). Everything else
    linked to the umbrella series was auto-attached in error.
    """
    from sqlalchemy import text
    async with SessionLocal() as session:
        result = await session.exec(text(
            "DELETE FROM comicseries "
            "WHERE series_id IN ("
            "    SELECT id FROM series WHERE name = 'Star Wars Movie Adaptations'"
            ") "
            "AND comic_id IN ("
            "    SELECT id FROM comic "
            "    WHERE lower(title) NOT LIKE '%adaptation%' "
            "      AND lower(title) NOT LIKE '%graphic novel%'"
            ")"
        ))
        await session.commit()
        return int(result.rowcount or 0)


async def backfill_prune_dangling_comicseries() -> int:
    """Delete ComicSeries / ComicContainment rows whose comic_id
    refers to a comic that no longer exists. Defensive against past
    delete paths that didn't clean up link tables — leaves the DB
    in a consistent state so the orphan-prune logic in comic_delete
    can do its job without being misled by ghost link rows.
    Returns total rows deleted across both tables.
    """
    from sqlalchemy import text
    total = 0
    async with SessionLocal() as session:
        for table, col in [
            ("comicseries", "comic_id"),
            ("comiccontainment", "parent_id"),
            ("comiccontainment", "child_id"),
        ]:
            r = await session.exec(text(
                f"DELETE FROM {table} WHERE {col} NOT IN (SELECT id FROM comic)"
            ))
            total += int(r.rowcount or 0)
        await session.commit()
    return total


async def backfill_comic_series_links() -> int:
    """Mirror every Comic.series_id value into the ComicSeries link
    table AND ensure exactly one row per comic is flagged `is_primary`
    (the one whose `series_id` matches the current `Comic.series_id`).
    Idempotent — safe on every cold start.

    Two-step:
      1. INSERT OR IGNORE the primary link for every Comic.series_id.
      2. Normalise the `is_primary` flag across all rows so that
         exactly the link matching `Comic.series_id` is True and every
         other row (including stale primaries left over when
         Comic.series_id was reassigned by /series/{id}/auto-link's
         backfill-rename or by the merge UI) becomes False.

    Step 2 is the fix for the "comic shows multiple PRIMARY badges"
    bug: previously when an auto-link rename moved Comic.series_id
    from S1 to S2, the backfill happily added (comic, S2,
    is_primary=True) but left (comic, S1, is_primary=True) in place,
    so the comic detail page rendered both as primary.

    Returns rows added by step 1. Step 2's UPDATE rowcount is noisy
    (touches every link) and not surfaced.
    """
    from sqlalchemy import text
    async with SessionLocal() as session:
        result = await session.exec(text(
            "INSERT OR IGNORE INTO comicseries "
            "  (comic_id, series_id, is_primary, created_at) "
            "SELECT id, series_id, 1, CURRENT_TIMESTAMP "
            "FROM comic WHERE series_id IS NOT NULL"
        ))
        # Normalise: a row is primary iff it matches Comic.series_id.
        # The CASE wrapper avoids producing NULL when Comic.series_id
        # is NULL — bare `series_id = NULL` evaluates to NULL in SQL
        # three-valued logic, which then violates the NOT NULL
        # constraint on comicseries.is_primary.
        await session.exec(text(
            "UPDATE comicseries SET is_primary = CASE "
            "WHEN series_id = ("
            "  SELECT series_id FROM comic WHERE comic.id = comicseries.comic_id"
            ") THEN 1 ELSE 0 END"
        ))
        await session.commit()
        return int(result.rowcount or 0)
