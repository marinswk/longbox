"""Re-pick a comic's upstream source match.

When the original auto-pick was wrong (e.g. CSV import grabbed the
single-issue Wookieepedia article instead of the TPB collection), the
detail page exposes a `/comic/{id}/repick` flow that re-runs the search
and lets the user select a different candidate. This module handles
the write side:

  * Refetch the chosen candidate (cached if recent).
  * Update `source` / `source_id` on the comic.
  * Force-overwrite source-derived fields (title, cover, description,
    format, canon, era, etc.) — that's the whole point of re-picking.
  * If the candidate names a different `series`, find-or-create that
    Series and reassign `comic.series_id`. Auto-prune the old series
    if it just lost its last comic.
  * Re-run creator / story-arc / character auto-tagging additively
    so existing user-curated tags survive.

Nothing here cancels existing copies, manual tags, or read history —
only metadata that the upstream owns gets refreshed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional

from fastapi import BackgroundTasks
from sqlalchemy import select as sa_select, delete as sa_delete
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import Comic, Series


@dataclass
class RepickOutcome:
    ok: bool
    message: str
    new_series_id: Optional[int] = None
    old_series_pruned: bool = False


# Book sources are refetched by the comic's ISBN (their stored
# `source_id` is an opaque book/volume key, and a refresh should
# re-merge Open Library + Google Books anyway — so a refresh of a
# sparse old Open Library comic now picks up Google Books data).
_BOOK_SOURCES = {"openlibrary", "googlebooks"}


async def _refetch_book_by_isbn(isbn: str):
    """Re-resolve a book-source comic via the merged ISBN lookup, so a
    refresh pulls the richest combined Open Library + Google Books
    record. Returns the candidate matching the ISBN, or None."""
    from app.services.aggregator import lookup_full
    res = await lookup_full(isbn, sources=_BOOK_SOURCES)
    target = (isbn or "").replace("-", "").strip()
    for c in res.candidates:
        if target in {c.isbn_13, c.isbn_10}:
            return c
    return res.candidates[0] if res.candidates else None


async def apply_repick(
    session: AsyncSession,
    comic: Comic,
    *,
    source: str,
    source_id: str,
    background: BackgroundTasks,
) -> RepickOutcome:
    """Replace the comic's upstream link with `(source, source_id)` and
    refresh every source-derived field. Returns a summary so the UI can
    render an honest flash banner."""
    if not source or not source_id:
        return RepickOutcome(ok=False, message="Missing source / source_id.")

    # Late imports — these modules sit deeper in the import graph.
    from app.routers.add import (
        _backfill_metadata, _download_and_store_cover, _ensure_tag,
        _get_or_create_publisher, _get_or_create_series, _persist_arcs,
        _persist_creators, _refetch_candidate,
    )
    from app.routers.detail import _autotag_from_candidate
    from app.services.csv_import import translate_format

    candidate = await _refetch_candidate(source, source_id)
    # Book sources (Open Library / Google Books) aren't handled by
    # `_refetch_candidate` (their source_id is an opaque key and they're
    # not re-fetched at save). Refresh them via the merged ISBN lookup
    # instead — this both fixes the old "couldn't reach openlibrary"
    # 502 AND enriches a sparse record with Google Books data. The
    # merged candidate may carry a richer primary source (Google Books),
    # which we adopt.
    if candidate is None and source in _BOOK_SOURCES:
        isbn = (comic.isbn_13 or comic.isbn_10 or "").strip()
        if isbn:
            candidate = await _refetch_book_by_isbn(isbn)
            if candidate is not None:
                source = candidate.source or source
                source_id = candidate.source_id or source_id
    if candidate is None:
        if source in _BOOK_SOURCES:
            return RepickOutcome(
                ok=False,
                message=(
                    f"{source} returned nothing for this comic's ISBN. "
                    "It may have no ISBN, or the source is down — try again later."
                ),
            )
        return RepickOutcome(
            ok=False,
            message=f"Couldn't reach {source} for {source_id!r}. Try again later.",
        )

    # 1. Source linkage swaps over first — even if everything else fails,
    #    at least the comic now points at the right upstream record.
    comic.source = source
    comic.source_id = source_id

    # 2. Force-refresh source-derived scalar fields. `_backfill_metadata`
    #    handles upc, collected_issues, format, language, timeline, era,
    #    canon, description — but it specifically WON'T touch a non-empty
    #    description. Re-pick is an explicit "I'm wrong, replace it" so
    #    we override that conservatism for description too.
    if candidate.title:
        comic.title = candidate.title
    if candidate.issue_number:
        comic.issue_number = candidate.issue_number
    # Cover: take the new URL and queue a fresh local download. Clearing
    # `cover_url_local` here means the detail template falls back to the
    # new remote URL right away, instead of showing the old downloaded
    # file until the background task finishes.
    if candidate.cover_url:
        comic.cover_url_remote = candidate.cover_url
        comic.cover_url_local = None
        background.add_task(
            _download_and_store_cover, comic.id, candidate.cover_url,
        )
    # Description force-replace (see comment above).
    if candidate.description:
        comic.description = candidate.description

    await _backfill_metadata(session, comic, candidate, force=True)
    if candidate.format:
        comic.format = translate_format(candidate.format)

    # 3. Series reassignment. The candidate may name a different series
    #    (the most common case: TPB article → trade series, vs single-
    #    issue article → singles series).
    new_series_id: Optional[int] = comic.series_id
    old_series_id = comic.series_id
    if candidate.series:
        publisher_row = None
        if candidate.publisher:
            publisher_row = await _get_or_create_publisher(
                session, candidate.publisher,
            )
        series_row = await _get_or_create_series(
            session, candidate.series,
            publisher_row.id if publisher_row else None,
        )
        if series_row and series_row.id != comic.series_id:
            comic.series_id = series_row.id
            new_series_id = series_row.id

    comic.updated_at = datetime.now(UTC)
    session.add(comic)
    await session.commit()

    # 4. Re-run additive enrichment. Idempotent helpers — repeating them
    #    won't double-apply existing creators / arcs / character tags.
    if candidate.creators:
        await _persist_creators(session, comic.id, candidate.creators)
    if candidate.story_arcs:
        await _persist_arcs(session, comic.id, candidate.story_arcs)
    if source == "wookieepedia":
        await _ensure_tag(session, comic.id, "star wars")
        if candidate.canon:
            await _ensure_tag(session, comic.id, candidate.canon)
    await _autotag_from_candidate(session, comic.id, candidate)

    # Re-derive multi-series memberships from the refreshed
    # collected_issues list. Idempotent.
    from app.routers.add import _attach_inferred_series
    await _attach_inferred_series(comic.id)

    # 5. Auto-prune the previous series if the move left it empty.
    pruned = False
    if old_series_id and old_series_id != new_series_id:
        remaining = (await session.exec(
            sa_select(Comic.id).where(Comic.series_id == old_series_id).limit(1)
        )).first()
        if remaining is None:
            await session.exec(
                sa_delete(Series).where(Series.id == old_series_id)
            )
            await session.commit()
            pruned = True

    return RepickOutcome(
        ok=True,
        message="Re-pick applied. Source-owned fields refreshed.",
        new_series_id=new_series_id,
        old_series_pruned=pruned,
    )
