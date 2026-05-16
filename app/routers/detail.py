"""Comic detail page + per-Copy CRUD.

GET  /comic/{id}                       — full detail page.
POST /comic/{id}/edit                  — update Comic fields, returns the meta partial.
POST /comic/{id}/delete                — cascades Copies, redirects to /library.
POST /comic/{id}/copies                — add a new Copy, returns copies partial.
POST /comic/{id}/copies/{copy_id}/edit — update a Copy, returns copies partial.
POST /comic/{id}/copies/{copy_id}/delete — delete a Copy, returns copies partial.

Each "edit" route accepts the same form fields and returns the appropriate
partial so HTMX can swap a chunk of the page in place. Full-page POSTs (delete
comic) use HX-Redirect so the browser navigates away.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Optional

import hashlib
import re

from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form, HTTPException,
    Request, UploadFile,
)
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.services import covers
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import (
    Comic,
    ComicArc,
    ComicCreator,
    ComicTag,
    Copy,
    Creator,
    Publisher,
    Series,
    StoryArc,
    Tag,
)
from app.routers.add import (
    _backfill_metadata,
    _ensure_tag,
    _persist_arcs,
    _persist_creators,
    _refetch_candidate,
)


_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _clean_character_name(name: str) -> str:
    """Strip the trailing parenthetical disambiguator that CV / Wookieepedia
    add to disambiguate same-name characters across continuities, e.g.
    "Han Solo (Earth-616)" or "Boba Fett (Star Wars)" → just the name.

    We keep characters with embedded punctuation otherwise — "C-3PO",
    "Obi-Wan Kenobi", "Mara Jade Skywalker" all pass through unchanged.
    """
    return _PAREN_RE.sub("", name or "").strip()


async def _autotag_from_candidate(session: AsyncSession, comic_id: int, candidate) -> int:
    """Apply chars / story-arcs from a candidate as tags. Returns the count
    of *newly added* links by diffing the comic's tag set before and after.
    Used by both the on-save flow and the `/comic/{id}/auto-tag` retro-fill
    endpoint.

    Notes on what's applied:
    * `chars: NAME` for characters (parenthetical disambiguators stripped).
    * Bare names for story arcs.
    * Concepts are intentionally NOT applied — CV's `concept_credits`
      returns noisy / abstract terms ("blaster", "war") that pollute the
      tag list. If users want those they can add them manually.
    """
    from app.models import ComicTag, Tag
    from sqlmodel import select as _select

    async def _existing_count() -> int:
        return len((await session.exec(
            _select(ComicTag).where(ComicTag.comic_id == comic_id)
        )).all())

    before = await _existing_count()
    for arc in (candidate.story_arcs or [])[:10]:
        await _ensure_tag(session, comic_id, arc)
    for ch in (candidate.characters or [])[:10]:
        cleaned = _clean_character_name(ch)
        if cleaned:
            await _ensure_tag(session, comic_id, f"chars: {cleaned}")
    after = await _existing_count()
    return max(0, after - before)

router = APIRouter(tags=["detail"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _parse_float(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value and value.isdigit():
        return int(value)
    return None


async def _load(session: AsyncSession, comic_id: int) -> dict:
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    series = await session.get(Series, comic.series_id) if comic.series_id else None
    publisher = (
        await session.get(Publisher, series.publisher_id)
        if series and series.publisher_id
        else None
    )
    copies_result = await session.exec(
        select(Copy).where(Copy.comic_id == comic_id).order_by(Copy.id.asc())
    )
    copies = list(copies_result.all())
    tags_result = await session.exec(
        select(Tag)
        .join(ComicTag, ComicTag.tag_id == Tag.id)
        .where(ComicTag.comic_id == comic_id)
        .order_by(Tag.name)
    )
    tags = list(tags_result.all())

    creators_result = await session.exec(
        select(Creator.name, ComicCreator.role)
        .join(ComicCreator, ComicCreator.creator_id == Creator.id)
        .where(ComicCreator.comic_id == comic_id)
        .order_by(ComicCreator.role, Creator.name)
    )
    grouped: dict[str, list[str]] = {}
    for name, role in creators_result.all():
        grouped.setdefault(role or "creator", []).append(name)
    # Stable presentation order — common comic-credit roles first.
    role_order = ["writer", "penciller", "inker", "letterer", "colorist", "cover artist", "editor"]
    creators_sections = []
    for role in role_order:
        if role in grouped:
            creators_sections.append((role, grouped.pop(role)))
    for role, names in sorted(grouped.items()):
        creators_sections.append((role, names))

    arcs_result = await session.exec(
        select(StoryArc.name)
        .join(ComicArc, ComicArc.arc_id == StoryArc.id)
        .where(ComicArc.comic_id == comic_id)
        .order_by(StoryArc.name)
    )
    arcs = list(arcs_result.all())

    # Pre-parse `collected_issues` so the template can render linkable vs
    # prose entries differently. Done here (not in Jinja) so the
    # heuristic + tests live in one place.
    from app.services.collected_issues import parse_entries
    collected = parse_entries(comic.collected_issues)

    # Containment: child Comics this one collects (TPBs inside an
    # omnibus, etc.). Loaded here so the page renders without a
    # second HTMX round-trip. Each child is tagged with `owned` so
    # the template can grey out stub-only references.
    from app.models import ComicContainment, ComicSeries
    from sqlalchemy import func as _func
    contain_rows = (await session.exec(
        select(Comic, ComicContainment)
        .join(ComicContainment, ComicContainment.child_id == Comic.id)
        .where(ComicContainment.parent_id == comic_id)
        .order_by(ComicContainment.position.asc(), Comic.id.asc())
    )).all()
    contains_children: list[dict] = []
    for child, _link in contain_rows:
        n_copies = (await session.exec(
            select(_func.count(Copy.id)).where(Copy.comic_id == child.id)
        )).first()
        copies_n = n_copies[0] if isinstance(n_copies, tuple) else (n_copies or 0)
        contains_children.append({"comic": child, "owned": int(copies_n or 0) > 0})

    # "Covered by" — every parent Comic in the library that lists
    # this one as a contained child. Lets the user see "the issues in
    # this TPB are also in my owned Omnibus X" at a glance.
    covered_by_rows = (await session.exec(
        select(Comic)
        .join(ComicContainment, ComicContainment.parent_id == Comic.id)
        .where(ComicContainment.child_id == comic_id)
        .order_by(Comic.title)
    )).all()
    covered_by = list(covered_by_rows)

    # Multi-series links. The primary series is always first, then
    # any extra series attached via the multi-series form.
    series_rows: list[tuple] = []
    seen_series_ids: set[int] = set()
    if comic.series_id is not None and series is not None:
        series_rows.append((series, True))
        seen_series_ids.add(series.id)
    extra_rows = (await session.exec(
        select(Series, ComicSeries.is_primary)
        .join(ComicSeries, ComicSeries.series_id == Series.id)
        .where(ComicSeries.comic_id == comic_id)
        .order_by(Series.name)
    )).all()
    for s, is_primary in extra_rows:
        if s.id in seen_series_ids:
            continue
        seen_series_ids.add(s.id)
        series_rows.append((s, bool(is_primary)))

    return {
        "comic": comic, "series": series, "publisher": publisher,
        "copies": copies, "tags": tags, "comic_id": comic_id,
        "creators_sections": creators_sections,
        "arcs": arcs,
        "collected_entries": collected,
        "contains_children": contains_children,
        "covered_by": covered_by,
        "series_rows": series_rows,
    }


@router.get("/comic/{comic_id}", response_class=HTMLResponse)
async def comic_detail(
    comic_id: int, request: Request, session: SessionDep,
    flash: str = "",
) -> HTMLResponse:
    ctx = await _load(session, comic_id)
    ctx["flash"] = flash[:200] if flash else None
    return templates.TemplateResponse(request, "comic_detail.html", ctx)


@router.post("/comic/{comic_id}/edit", response_class=HTMLResponse)
async def comic_edit(
    comic_id: int,
    request: Request,
    session: SessionDep,
    title: str = Form(""),
    issue_number: str = Form(""),
    variant: str = Form(""),
    cover_date: str = Form(""),
    page_count: str = Form(""),
    isbn_10: str = Form(""),
    isbn_13: str = Form(""),
    upc: str = Form(""),
    cover_url_remote: str = Form(""),
    description: str = Form(""),
    cover_price_eur: str = Form(""),
    format: str = Form(""),
    language: str = Form(""),
    canon: str = Form(""),
    era: str = Form(""),
    timeline: str = Form(""),
    collected_issues: str = Form(""),
    fandom: str = Form(""),
    fandom_new: str = Form(""),
    publisher: str = Form(""),
    series_name: str = Form(""),
) -> HTMLResponse:
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    comic.title = title or None
    comic.issue_number = issue_number or None
    comic.variant = variant or None
    comic.cover_date = _parse_date(cover_date)
    comic.page_count = _parse_int(page_count)
    comic.isbn_10 = isbn_10 or None
    comic.isbn_13 = isbn_13 or None
    comic.upc = upc or None
    comic.cover_url_remote = cover_url_remote or None
    comic.description = description or None
    comic.cover_price_eur = _parse_float(cover_price_eur)
    from app.services.csv_import import translate_format as _norm_format
    comic.format = _norm_format(format)
    comic.language = language or None
    comic.canon = canon or None
    comic.era = era or None
    comic.timeline = timeline or None
    comic.collected_issues = collected_issues or None
    # Fandom picker — same resolve rule as /add/save (free-text wins, but
    # `__NEW__` sentinel from the dropdown means "use fandom_new").
    from app.services.fandoms import normalize as _normalize_fandom
    if fandom == "__NEW__":
        comic.fandom = _normalize_fandom(fandom_new)
    else:
        comic.fandom = _normalize_fandom(fandom_new or fandom)

    # Series + publisher edits — these mutate the parent rows. Series rename
    # affects every other comic in that series; publisher rename moves the
    # series to a different publisher row (find-or-create by name). Empty
    # inputs are no-ops so users who only wanted to tweak title/etc. don't
    # accidentally orphan their series.
    series_name_clean = (series_name or "").strip()
    publisher_clean = (publisher or "").strip()
    if (series_name_clean or publisher_clean) and comic.series_id:
        from app.routers.add import (
            _get_or_create_publisher, _get_or_create_series,
        )
        # If the user typed a different series name, move the comic
        # (find-or-create), so we don't rewrite the parent series's name
        # and accidentally rename it for every sibling comic too.
        target_series_id: int | None = comic.series_id
        if series_name_clean:
            current_series = await session.get(Series, comic.series_id)
            if current_series is None or current_series.name != series_name_clean:
                pub_for_series = None
                if publisher_clean:
                    pub_row = await _get_or_create_publisher(session, publisher_clean)
                    pub_for_series = pub_row.id if pub_row else None
                elif current_series:
                    pub_for_series = current_series.publisher_id
                new_series = await _get_or_create_series(
                    session, series_name_clean, pub_for_series,
                )
                if new_series:
                    target_series_id = new_series.id
                    comic.series_id = new_series.id

        # Publisher edit applies to the (possibly newly-assigned) series.
        if publisher_clean and target_series_id is not None:
            ser = await session.get(Series, target_series_id)
            if ser is not None:
                pub_row = await _get_or_create_publisher(session, publisher_clean)
                if pub_row and ser.publisher_id != pub_row.id:
                    ser.publisher_id = pub_row.id
                    session.add(ser)

    comic.updated_at = datetime.now(UTC)
    session.add(comic)
    await session.commit()
    await session.refresh(comic)

    ctx = await _load(session, comic_id)
    return templates.TemplateResponse(request, "partials/_comic_meta.html", ctx)


@router.get("/comic/{comic_id}/edit", response_class=HTMLResponse)
async def comic_edit_form(
    comic_id: int, request: Request, session: SessionDep
) -> HTMLResponse:
    ctx = await _load(session, comic_id)
    from app.services.fandoms import list_fandoms
    ctx["fandoms"] = await list_fandoms(session)
    ctx["current_fandom"] = ctx["comic"].fandom
    return templates.TemplateResponse(request, "partials/_comic_meta_form.html", ctx)


_ALLOWED_COVER_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}
_MAX_COVER_BYTES = 10 * 1024 * 1024  # 10 MB hard cap


@router.post("/comic/{comic_id}/cover/upload", response_class=HTMLResponse)
async def upload_cover(
    comic_id: int,
    request: Request,
    session: SessionDep,
    cover: UploadFile = File(...),
) -> HTMLResponse:
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")

    ext = _ALLOWED_COVER_TYPES.get((cover.content_type or "").lower())
    if ext is None:
        raise HTTPException(status_code=415, detail="cover must be a JPEG, PNG, WebP, or GIF")

    data = await cover.read(_MAX_COVER_BYTES + 1)
    if len(data) > _MAX_COVER_BYTES:
        raise HTTPException(status_code=413, detail="cover image must be 10 MB or smaller")
    if not data:
        raise HTTPException(status_code=400, detail="empty file")

    # Hash on content so re-uploading the same image stays idempotent.
    digest = hashlib.sha1(data).hexdigest()
    target = covers.covers_dir() / f"{digest}{ext}"
    if not target.exists():
        target.write_bytes(data)

    comic.cover_url_local = covers.served_url_for(target)
    comic.updated_at = datetime.now(UTC)
    session.add(comic)
    await session.commit()
    await session.refresh(comic)

    ctx = await _load(session, comic_id)
    return templates.TemplateResponse(request, "comic_detail.html", ctx, headers={"HX-Refresh": "true"})


@router.post("/comic/{comic_id}/refresh")
async def comic_refresh(
    comic_id: int,
    session: SessionDep,
    background: BackgroundTasks,
    source: str = Form(""),
    source_id: str = Form(""),
) -> Response:
    """Re-resolve the upstream candidate and force-overwrite every
    source-derived column. Same end-state as `/comic/{id}/repick/apply`
    but without changing source/source_id (unless the form supplies new
    values). Triggers a fresh cover download in the background.

    Submitted source/source_id override what's stored on the comic, so
    the user can adopt a source for a manually-entered row.
    """
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")

    src = (source or comic.source or "").strip()
    sid = (source_id or comic.source_id or "").strip()
    if not src or not sid:
        raise HTTPException(status_code=400, detail="source and source_id required")

    # Re-use the apply_repick pipeline so refresh ↔ repick can never
    # drift out of sync on what counts as "source-owned" — both flows
    # do the same force-overwrite + series reassignment.
    from app.services.repick import apply_repick
    outcome = await apply_repick(
        session, comic, source=src, source_id=sid, background=background,
    )
    if not outcome.ok:
        raise HTTPException(status_code=502, detail=outcome.message)

    return Response(status_code=204, headers={"HX-Refresh": "true"})


@router.post("/comic/{comic_id}/rederive-series")
async def comic_rederive_series(
    comic_id: int, session: SessionDep,
) -> Response:
    """Re-run the multi-series inference + empty-series cleanup for
    this comic. Useful when the user manually edits collected_issues
    or when the parser has been improved since the original save —
    saves them from having to delete + re-add the comic.

    Idempotent: existing canonical Series rows stay; only adds the
    ones the current parser can resolve from the comic's
    collected_issues + sweeps any leftover empty rows.
    """
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")

    from app.routers.add import _attach_inferred_series
    from app.services.fandoms import backfill_prune_empty_inferred_series

    await _attach_inferred_series(comic_id)
    await backfill_prune_empty_inferred_series()

    return Response(
        status_code=204,
        headers={"HX-Refresh": "true"},
    )


@router.post("/comic/{comic_id}/delete")
async def comic_delete(comic_id: int, session: SessionDep) -> Response:
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")

    # Snapshot every series this comic was linked to — primary FK +
    # multi-series link table. We'll check each one for orphan-status
    # after the comic is gone.
    from app.models import ComicSeries, ComicContainment
    from sqlalchemy import func as _func, delete as sa_delete
    linked_series_ids: set[int] = set()
    if comic.series_id is not None:
        linked_series_ids.add(comic.series_id)
    link_rows = (await session.exec(
        select(ComicSeries.series_id).where(ComicSeries.comic_id == comic_id)
    )).all()
    for r in link_rows:
        linked_series_ids.add(r if isinstance(r, int) else r[0])

    # Cascade: drop link rows (ComicSeries + ComicContainment in both
    # directions) before the comic itself, so we don't leave dangling
    # FK references.
    await session.exec(sa_delete(ComicSeries).where(ComicSeries.comic_id == comic_id))
    await session.exec(sa_delete(ComicContainment).where(ComicContainment.parent_id == comic_id))
    await session.exec(sa_delete(ComicContainment).where(ComicContainment.child_id == comic_id))
    copies = await session.exec(select(Copy).where(Copy.comic_id == comic_id))
    for c in copies.all():
        await session.delete(c)
    await session.delete(comic)
    await session.commit()

    # Auto-prune every now-orphan series: any of the previously-linked
    # series rows that no longer has any Comic referencing it (via
    # Comic.series_id OR ComicSeries pointing at a still-existing
    # comic) gets deleted so the library facets / dropdowns don't
    # keep ghost entries. Publishers are left alone — they can be
    # referenced by other series we still have.
    #
    # The link-count JOINs to Comic so dangling ComicSeries rows
    # (pointing at comic_ids that no longer exist) don't accidentally
    # protect the series from being pruned.
    for sid in linked_series_ids:
        primary_refs = (await session.exec(
            select(_func.count(Comic.id)).where(Comic.series_id == sid)
        )).first()
        primary_n = primary_refs[0] if isinstance(primary_refs, tuple) else primary_refs
        link_refs = (await session.exec(
            select(_func.count())
            .select_from(ComicSeries)
            .join(Comic, Comic.id == ComicSeries.comic_id)
            .where(ComicSeries.series_id == sid)
        )).first()
        link_n = link_refs[0] if isinstance(link_refs, tuple) else link_refs
        if int(primary_n or 0) == 0 and int(link_n or 0) == 0:
            # Drop any dangling link rows that point at gone comics
            # before deleting the series — keeps the FK cleanup tidy.
            await session.exec(sa_delete(ComicSeries).where(ComicSeries.series_id == sid))
            ghost = await session.get(Series, sid)
            if ghost is not None:
                await session.delete(ghost)
    await session.commit()

    return Response(status_code=204, headers={"HX-Redirect": "/library"})


@router.post("/comic/{comic_id}/copies", response_class=HTMLResponse)
async def add_copy(
    comic_id: int,
    request: Request,
    session: SessionDep,
    condition: str = Form(""),
    storage_location: str = Form(""),
    price_paid_eur: str = Form(""),
    purchase_date: str = Form(""),
    read_status: str = Form(""),
    notes: str = Form(""),
) -> HTMLResponse:
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    copy = Copy(
        comic_id=comic_id,
        condition=condition or None,
        storage_location=storage_location or None,
        price_paid_eur=_parse_float(price_paid_eur),
        purchase_date=_parse_date(purchase_date),
        read_status=read_status or None,
        notes=notes or None,
    )
    session.add(copy)
    await session.commit()
    ctx = await _load(session, comic_id)
    return templates.TemplateResponse(request, "partials/_copies_section.html", ctx)


@router.post("/comic/{comic_id}/copies/{copy_id}/edit", response_class=HTMLResponse)
async def edit_copy(
    comic_id: int,
    copy_id: int,
    request: Request,
    session: SessionDep,
    condition: str = Form(""),
    storage_location: str = Form(""),
    price_paid_eur: str = Form(""),
    purchase_date: str = Form(""),
    read_status: str = Form(""),
    date_read: str = Form(""),
    notes: str = Form(""),
) -> HTMLResponse:
    copy = await session.get(Copy, copy_id)
    if copy is None or copy.comic_id != comic_id:
        raise HTTPException(status_code=404, detail="copy not found")
    copy.condition = condition or None
    copy.storage_location = storage_location or None
    copy.price_paid_eur = _parse_float(price_paid_eur)
    copy.purchase_date = _parse_date(purchase_date)
    copy.read_status = read_status or None
    copy.date_read = _parse_date(date_read)
    copy.notes = notes or None
    session.add(copy)
    await session.commit()
    ctx = await _load(session, comic_id)
    return templates.TemplateResponse(request, "partials/_copies_section.html", ctx)


@router.post("/comic/{comic_id}/mark-read", response_class=HTMLResponse)
async def quick_mark_read(
    comic_id: int, request: Request, session: SessionDep,
) -> HTMLResponse:
    """Quick-action: flip the first not-yet-read copy to `read` with
    `date_read=today`. If every copy is already read, this is a no-op
    (re-renders the partial with no changes).

    The button on the detail page targets `#copies-section`, so HTMX
    can swap just that block in place.
    """
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    # NULL `read_status` means "not yet set" — must be treated as not-read.
    # Plain `!=` produces NULL in SQL when one side is NULL, so we have to
    # OR the IS NULL branch in explicitly.
    target = (
        await session.exec(
            select(Copy).where(
                Copy.comic_id == comic_id,
                or_(Copy.read_status.is_(None), Copy.read_status != "read"),
            ).order_by(Copy.id.asc()).limit(1)
        )
    ).first()
    if target is not None:
        target.read_status = "read"
        if target.date_read is None:
            target.date_read = datetime.now(UTC).date()
        session.add(target)
        await session.commit()
    ctx = await _load(session, comic_id)
    return templates.TemplateResponse(request, "partials/_copies_section.html", ctx)


@router.post("/comic/{comic_id}/copies/{copy_id}/delete", response_class=HTMLResponse)
async def delete_copy(
    comic_id: int, copy_id: int, request: Request, session: SessionDep
) -> HTMLResponse:
    copy = await session.get(Copy, copy_id)
    if copy is None or copy.comic_id != comic_id:
        raise HTTPException(status_code=404, detail="copy not found")
    await session.delete(copy)
    await session.commit()
    ctx = await _load(session, comic_id)
    return templates.TemplateResponse(request, "partials/_copies_section.html", ctx)


# ---------------------------------------------------------------------------
# Re-pick upstream source — separate page at /comic/{id}/repick
# ---------------------------------------------------------------------------


_REPICK_SOURCES = ("wookieepedia", "comicvine", "metron", "openlibrary")
_REPICK_LIMIT = 50


def _candidate_to_dict(c) -> dict:
    """Whitelist of fields the repick UI cares about."""
    return {
        "source": c.source,
        "source_id": c.source_id,
        "title": c.title,
        "series": c.series,
        "issue_number": c.issue_number,
        "publisher": c.publisher,
        "cover_date": c.cover_date,
        "cover_url": c.cover_url,
        "format": c.format,
    }


def _default_repick_query(comic: Comic, series: Series | None) -> str:
    """Best-guess freeform query to seed the search box from the comic's
    own fields. Series + title is the most useful starting point;
    issue_number is appended when set."""
    parts = []
    if series and series.name:
        parts.append(series.name)
    if comic.title and (not parts or comic.title.lower() not in parts[0].lower()):
        parts.append(comic.title)
    if comic.issue_number:
        parts.append(f"#{comic.issue_number}")
    return " ".join(parts).strip()


async def _run_repick_search(
    *, comic: Comic, query: str | None, sources: list[str],
) -> tuple[list[dict], list[str]]:
    """Run the multi-field search using either the user-typed query or
    the comic's existing fields. Returns (candidate-dicts, rate-limited-source-list)."""
    from app.services.aggregator import find_candidates_multi
    chosen = sources or list(_REPICK_SOURCES)
    if query and query.strip():
        result = await find_candidates_multi(
            custom_query=query.strip(),
            sources=chosen,
            limit=_REPICK_LIMIT,
        )
    else:
        # Auto-search from the comic's own fields. Pull the series name so
        # we can seed `series=` even when comic.title only carries the
        # issue suffix ("The High Republic 1").
        series_name = None
        if comic.series_id:
            from app.models import Series as _S
            from sqlmodel import select as _sel
            # We don't have a session here; pull the series via the
            # caller's session pattern. Instead, the caller passes
            # series_name via query when needed.
            series_name = None
        # Fallback purely on what the comic carries.
        result = await find_candidates_multi(
            series=None,
            title=comic.title,
            issue_number=comic.issue_number,
            isbn=comic.isbn_13,
            upc=comic.upc,
            sources=chosen,
            limit=_REPICK_LIMIT,
        )
    return [_candidate_to_dict(c) for c in result.candidates], list(result.rate_limited or [])


@router.get("/comic/{comic_id}/repick", response_class=HTMLResponse)
async def comic_repick(
    comic_id: int, request: Request, session: SessionDep,
    q: str = "",
) -> HTMLResponse:
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    series = await session.get(Series, comic.series_id) if comic.series_id else None

    # Seed the search box. URL `?q=…` wins; otherwise derive from existing fields.
    seeded_q = q.strip() or _default_repick_query(comic, series)

    # Default source selection: whichever source the comic is currently
    # linked to (so the user can find a sibling article in the same wiki),
    # plus whatever else is configured.
    from app.services.import_sources import build_source_tiles
    tiles = build_source_tiles([], {})
    selected_sources = [t.key for t in tiles if t.configured]
    if comic.source and comic.source not in selected_sources:
        selected_sources.append(comic.source)

    # Run an initial search using the seeded query.
    candidates, rate_limited = await _run_repick_search(
        comic=comic, query=seeded_q, sources=selected_sources,
    )

    return templates.TemplateResponse(
        request, "comic_repick.html",
        {
            "comic": comic,
            "series": series,
            "tiles": tiles,
            "selected_sources": selected_sources,
            "candidates": candidates,
            "rate_limited": rate_limited,
            "seeded_q": seeded_q,
        },
    )


@router.post("/comic/{comic_id}/repick/search", response_class=HTMLResponse)
async def comic_repick_search(
    comic_id: int, request: Request, session: SessionDep,
) -> HTMLResponse:
    """Re-run the search with whatever the user typed + which sources
    they ticked. Returns the candidates partial only (HTMX swap)."""
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    form = await request.form()
    query = form.get("q") or ""
    sources = [k for k in _REPICK_SOURCES if form.get(f"source[{k}]") == "on"]
    candidates, rate_limited = await _run_repick_search(
        comic=comic, query=query, sources=sources,
    )
    return templates.TemplateResponse(
        request, "partials/_repick_candidates.html",
        {
            "comic": comic,
            "candidates": candidates,
            "rate_limited": rate_limited,
        },
    )


@router.post("/comic/{comic_id}/repick/apply", response_model=None)
async def comic_repick_apply(
    comic_id: int,
    request: Request,
    session: SessionDep,
    background: BackgroundTasks,
    source: str = Form(""),
    source_id: str = Form(""),
):
    comic = await session.get(Comic, comic_id)
    if comic is None:
        raise HTTPException(status_code=404, detail="comic not found")
    from app.services.repick import apply_repick
    outcome = await apply_repick(
        session, comic,
        source=source, source_id=source_id, background=background,
    )
    # Pass a flash message via query string so the detail page can render
    # it without us threading session storage through.
    from urllib.parse import quote
    flash = quote(outcome.message)[:200]
    return RedirectResponse(
        url=f"/comic/{comic_id}?flash={flash}", status_code=303,
    )
