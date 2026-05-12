"""Commit step (5) of the CSV import wizard.

For each ImportRow not in the (skipped|errored|committed) terminal states,
this module creates a Comic + Copy + the usual creators/arcs/tags chain
that the regular `/add/save` flow produces. Re-uses the helpers from
`app.routers.add` and `app.routers.detail` so the import behaves identically
to a manual save where it can.

Behaviors of note:

  * "matched"/"multi" rows with a chosen source: the candidate is re-fetched
    via `_refetch_candidate` so we get the rich CV/Metron/WP fields even
    though only the (source, source_id) pair was stored on the row.
  * "as_is" rows: skip the upstream fetch entirely, write a Comic with just
    the mapped CSV fields. No creators/arcs/auto-tags from upstream.
  * "skipped"/"errored"/"committed" rows: ignored.
  * Cover downloads are scheduled via the FastAPI BackgroundTasks queue
    so the commit response returns fast.
  * Each row commit runs in its own try/except — a single bad row sets
    `status="errored"` on that row but doesn't stop the rest of the batch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Optional

from fastapi import BackgroundTasks
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import Comic, Copy, ImportRow, ImportSession
from app.services.csv_import import translate_format
from app.services.fandoms import normalize as _normalize_fandom


@dataclass
class CommitSummary:
    committed: int = 0
    skipped: int = 0
    errored: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)
    comic_ids: list[int] = field(default_factory=list)


def _date_from_year(year_str: str | None):
    """If the CSV gave us only a year (or "2015 (2nd print)"), build a
    Jan-1 cover_date so the year survives. Returns None on parse failure."""
    if not year_str:
        return None
    digits = "".join(ch for ch in year_str if ch.isdigit())[:4]
    if len(digits) != 4:
        return None
    try:
        from datetime import date
        return date(int(digits), 1, 1)
    except (TypeError, ValueError):
        return None


def _mapped(raw: dict, column_map: dict) -> dict:
    out: dict = {}
    for our_key, csv_header in column_map.items():
        v = (raw.get(csv_header) or "").strip()
        if v:
            out[our_key] = v
    return out


async def _commit_row(
    session: AsyncSession,
    sess: ImportSession,
    row: ImportRow,
    column_map: dict,
    config: dict,
    background: BackgroundTasks,
) -> Optional[int]:
    """Save a single row. Returns the new Comic.id on success, None if the
    row was skipped (e.g. terminal status, or `mapped` produced no usable
    fields). Raises on hard failures so the caller can mark the row errored."""
    # Pull deps lazily — these modules import each other and circular imports
    # are easier to avoid by deferring the pull to call time.
    from app.routers.add import (
        _get_or_create_publisher, _get_or_create_series,
        _persist_creators, _persist_arcs, _backfill_metadata,
        _refetch_candidate, _ensure_tag, _download_and_store_cover,
    )
    from app.routers.detail import _autotag_from_candidate

    raw = json.loads(row.raw)
    mapped = _mapped(raw, column_map)
    if not mapped:
        # Nothing to save (probably a row whose mapped columns are all empty).
        return None

    # Re-fetch candidate when the row picked one. Tolerates rate-limits /
    # timeouts: refetch returns None which we handle below.
    candidate = None
    if row.chosen_source and row.chosen_source_id and row.status != "as_is":
        candidate = await _refetch_candidate(row.chosen_source, row.chosen_source_id)

    # Resolve scalar fields. Candidate wins for richer data; CSV mapped
    # value fills gaps (or wins for fandom, which the candidate doesn't
    # carry on its own).
    title = (candidate.title if candidate else None) or mapped.get("title")
    series_name = (
        (candidate.series if candidate else None)
        or mapped.get("series")
        or (title if mapped.get("publisher") else None)
    )
    publisher = (candidate.publisher if candidate else None) or mapped.get("publisher")
    issue_number = (candidate.issue_number if candidate else None) or mapped.get("issue_number")
    isbn_13 = (candidate.isbn_13 if candidate else None) or mapped.get("isbn_13")
    upc = (candidate.upc if candidate else None) or mapped.get("upc")
    cover_url_remote = candidate.cover_url if candidate else None
    description = candidate.description if candidate else None
    fmt = (candidate.format if candidate else None) or translate_format(mapped.get("format"))
    cover_date = None
    if candidate and candidate.cover_date:
        # Re-use add's parser via Comic.cover_date typed assignment below.
        from app.routers.detail import _parse_date as _pd
        cover_date = _pd(candidate.cover_date)
    if cover_date is None:
        cover_date = _date_from_year(mapped.get("year"))

    # Fandom: CSV mapped column wins when auto_tag_fandom is on; else fall
    # back to "star wars" for Wookieepedia hits, else None.
    fandom_value: Optional[str] = None
    if config.get("auto_tag_fandom", True):
        fandom_value = _normalize_fandom(mapped.get("fandom"))
    if not fandom_value and row.chosen_source == "wookieepedia":
        fandom_value = "star wars"

    publisher_row = await _get_or_create_publisher(session, publisher or None)
    series_row = await _get_or_create_series(
        session, series_name or None,
        publisher_row.id if publisher_row else None,
    )

    comic = Comic(
        series_id=series_row.id if series_row else None,
        title=title or None,
        issue_number=issue_number or None,
        cover_date=cover_date,
        isbn_13=isbn_13 or None,
        upc=upc or None,
        cover_url_remote=cover_url_remote or None,
        description=description or None,
        source=row.chosen_source or None,
        source_id=row.chosen_source_id or None,
        format=fmt or None,
        fandom=fandom_value,
        # Fields that only the CSV may have provided.
        collected_issues=mapped.get("collected_issues") or None,
        variant=mapped.get("variant") or None,
    )
    session.add(comic)
    await session.commit()
    await session.refresh(comic)

    if comic.cover_url_remote:
        background.add_task(_download_and_store_cover, comic.id, comic.cover_url_remote)

    # Backfill from candidate (creators/arcs/canon/era/auto-tags). When
    # there's no candidate (as-is rows) we skip everything that needs one.
    if candidate is not None:
        if candidate.creators:
            await _persist_creators(session, comic.id, candidate.creators)
        if candidate.story_arcs:
            await _persist_arcs(session, comic.id, candidate.story_arcs)
        await _backfill_metadata(session, comic, candidate)
        if row.chosen_source == "wookieepedia":
            await _ensure_tag(session, comic.id, "star wars")
            if candidate.canon:
                await _ensure_tag(session, comic.id, candidate.canon)
        await _autotag_from_candidate(session, comic.id, candidate)

    # Optional publisher tag (from config knob).
    if config.get("auto_tag_publisher") and publisher_row:
        await _ensure_tag(session, comic.id, publisher_row.name)

    # Empty Copy so the comic shows up in counts and the user can fill in
    # condition/storage later, matching /add/save's behavior.
    session.add(Copy(comic_id=comic.id, purchase_date=datetime.now(UTC).date()))
    await session.commit()
    return comic.id


async def commit_session(
    session: AsyncSession,
    sess: ImportSession,
    background: BackgroundTasks,
) -> CommitSummary:
    """Process every committable row in the session."""
    from sqlmodel import select
    rows = (await session.exec(
        select(ImportRow)
        .where(ImportRow.session_id == sess.id)
        .order_by(ImportRow.row_index.asc())
    )).all()

    column_map = json.loads(sess.column_map or "{}")
    config = json.loads(sess.config or "{}")
    summary = CommitSummary()

    for row in rows:
        if row.status in ("skipped", "errored", "committed"):
            summary.skipped += 1
            continue
        if row.status not in ("matched", "multi", "as_is", "not_found"):
            # Defensive: a row stuck in `pending`/`multi` shouldn't reach
            # commit (the resolve page disables the button), but if it does
            # we still try.
            pass
        try:
            comic_id = await _commit_row(
                session, sess, row, column_map, config, background,
            )
            if comic_id is None:
                row.status = "skipped"
                summary.skipped += 1
            else:
                row.status = "committed"
                row.comic_id = comic_id
                row.error = None
                summary.committed += 1
                summary.comic_ids.append(comic_id)
            session.add(row)
            await session.commit()
        except Exception as exc:  # pragma: no cover — defensive
            row.status = "errored"
            row.error = str(exc)[:200]
            session.add(row)
            await session.commit()
            summary.errored += 1
            summary.errors.append((row.row_index, str(exc)[:200]))

    sess.state = "done"
    session.add(sess)
    await session.commit()
    return summary
