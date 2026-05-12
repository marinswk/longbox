"""Add-comic flow.

Four HTMX routes drive the funnel:
  GET  /add            — landing page with the identifier input.
  POST /add/lookup     — calls the aggregator, returns the picker partial.
  POST /add/confirm    — given a picked candidate, returns either the
                          confirm form (new comic) or the duplicate prompt.
  POST /add/save       — creates a Comic (or finds the existing one) and a Copy,
                          schedules the cover download, returns the success partial.

Duplicate detection (Phase 5 scope): match by ISBN-13, ISBN-10,
ComicVine ID, or Metron ID. Series-based matching arrives once the
relational tree (Publisher/Series rows) is populated by lookups.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

import re

from app.db import SessionLocal, get_session
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
from app.services import comicvine, covers, metron, wookieepedia
from app.services.aggregator import lookup_full as aggregator_lookup_full
from app.services.aggregator import search_text as aggregator_search_text
from app.services.schemas import LookupCandidate


def _slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "unknown"


async def _get_or_create_publisher(session: AsyncSession, name: Optional[str]) -> Optional[Publisher]:
    if not name:
        return None
    result = await session.exec(select(Publisher).where(Publisher.name == name))
    existing = result.first()
    if existing is not None:
        return existing
    pub = Publisher(name=name, slug=_slugify(name))
    session.add(pub)
    await session.flush()
    return pub


def _normalize_series_name(name: str) -> str:
    """Normalize a series name for dedup matching:
      * lowercase
      * Unicode em/en-dashes → plain hyphen
      * `--` / `---` → single hyphen
      * collapse whitespace around any hyphen (so "Foo - Bar" == "Foo-Bar")
      * collapse remaining whitespace runs
    Used only for the dedup probe; the persisted name keeps the user's
    original casing/punctuation.
    """
    s = name.lower()
    s = s.replace("—", "-").replace("–", "-")  # em + en-dash → hyphen
    s = re.sub(r"-{2,}", "-", s)               # collapse multi-hyphens
    s = re.sub(r"\s*-\s*", "-", s)             # collapse spaces AROUND a hyphen
    s = re.sub(r"\s+", " ", s).strip()
    return s


async def _get_or_create_series(
    session: AsyncSession, name: Optional[str], publisher_id: Optional[int]
) -> Optional[Series]:
    if not name:
        return None

    # Match by **normalized** name across ALL existing series, ignoring
    # the publisher in the lookup. This collapses cases like:
    #   "Star Wars: Jedi Knights" + Marvel Comics
    #   "Star Wars: Jedi Knights" + Marvel Worldwide, Incorporated
    # into a single series row instead of splitting it because the two
    # data sources reported the publisher slightly differently.
    target_norm = _normalize_series_name(name)
    rows = (await session.exec(select(Series))).all()

    # Prefer rows that already have comics attached; the empties came from
    # earlier mistakes and shouldn't dominate a fresh save.
    matches = [s for s in rows if _normalize_series_name(s.name) == target_norm]
    if matches:
        # Pick the one with the most comics; tie-break on lowest id.
        from sqlalchemy import func as _func
        counts: dict[int, int] = {}
        if matches:
            count_rows = await session.exec(
                select(Comic.series_id, _func.count(Comic.id))
                .where(Comic.series_id.in_([m.id for m in matches]))
                .group_by(Comic.series_id)
            )
            counts = {sid: n for sid, n in count_rows.all()}
        matches.sort(key=lambda s: (-counts.get(s.id, 0), s.id))
        chosen = matches[0]

        # Upgrade missing publisher_id if we now know one. Don't overwrite
        # an existing publisher — that's a real conflict and the merge UI
        # is the right tool to resolve it.
        if chosen.publisher_id is None and publisher_id is not None:
            chosen.publisher_id = publisher_id
            session.add(chosen)
            await session.flush()
        return chosen

    s = Series(name=name, publisher_id=publisher_id)
    session.add(s)
    await session.flush()
    return s

router = APIRouter(tags=["add"])

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


async def _find_duplicate(session: AsyncSession, *, isbn_13, isbn_10, upc, comicvine_id, metron_id) -> Optional[Comic]:
    clauses = []
    if isbn_13:
        clauses.append(Comic.isbn_13 == isbn_13)
    if isbn_10:
        clauses.append(Comic.isbn_10 == isbn_10)
    if upc:
        clauses.append(Comic.upc == upc)
    if comicvine_id:
        clauses.append(Comic.comicvine_id == comicvine_id)
    if metron_id:
        clauses.append(Comic.metron_id == metron_id)
    if not clauses:
        return None
    from sqlalchemy import or_
    result = await session.exec(select(Comic).where(or_(*clauses)).limit(1))
    return result.first()


async def _copy_count(session: AsyncSession, comic_id: int) -> int:
    result = await session.exec(
        select(func.count()).select_from(Copy).where(Copy.comic_id == comic_id)
    )
    return int(result.first() or 0)


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


@router.get("/add", response_class=HTMLResponse)
async def add_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "add.html")


@router.post("/add/lookup", response_class=HTMLResponse)
async def add_lookup(request: Request, identifier: str = Form(...)) -> HTMLResponse:
    result = await aggregator_lookup_full(identifier)
    return templates.TemplateResponse(
        request,
        "partials/_picker.html",
        {
            "identifier": identifier,
            "candidates": [c.model_dump() for c in result.candidates],
            "rate_limited": result.rate_limited,
        },
    )


# ---------------------------------------------------------------------------
# Text search (title / series / free-text across providers)
# ---------------------------------------------------------------------------

TEXT_SEARCH_PAGE_SIZE = 12


@router.api_route("/add/text-search", methods=["GET", "POST"], response_class=HTMLResponse)
async def add_text_search(
    request: Request,
    q: str = "",
    page: int = 1,
) -> HTMLResponse:
    """Free-text title / series search. Accepts both GET (used by the
    pagination links) and POST (form submit). Per-provider results are
    cached; pagination just slices the cached aggregate so flipping pages
    is a free re-render."""
    # FastAPI passes form fields and query params via the same arg names
    # for api_route, but on POST we want to read the form body.
    if request.method == "POST":
        form = await request.form()
        q = (form.get("q") or "").strip()
        try:
            page = int(form.get("page") or "1")
        except (TypeError, ValueError):
            page = 1
    q = (q or "").strip()
    page = max(1, page)

    if not q:
        return templates.TemplateResponse(
            request,
            "partials/_picker.html",
            {
                "identifier": "",
                "candidates": [],
                "rate_limited": [],
                "text_search": True,
                "q": "",
            },
        )

    result = await aggregator_search_text(q)
    total = len(result.candidates)
    page_size = TEXT_SEARCH_PAGE_SIZE
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    page_slice = result.candidates[start : start + page_size]

    return templates.TemplateResponse(
        request,
        "partials/_picker.html",
        {
            "identifier": q,
            "candidates": [c.model_dump() for c in page_slice],
            "rate_limited": result.rate_limited,
            "text_search": True,
            "q": q,
            "page": page,
            "total_pages": total_pages,
            "total": total,
        },
    )


@router.post("/add/confirm", response_class=HTMLResponse)
async def add_confirm(
    request: Request,
    session: SessionDep,
    title: str = Form(""),
    series: str = Form(""),
    issue_number: str = Form(""),
    publisher: str = Form(""),
    cover_date: str = Form(""),
    description: str = Form(""),
    page_count: str = Form(""),
    isbn_10: str = Form(""),
    isbn_13: str = Form(""),
    upc: str = Form(""),
    comicvine_id: str = Form(""),
    metron_id: str = Form(""),
    cover_url_remote: str = Form(""),
    source: str = Form(""),
    source_id: str = Form(""),
) -> HTMLResponse:
    fields = {
        "title": title or None,
        "series": series or None,
        "issue_number": issue_number or None,
        "publisher": publisher or None,
        "cover_date": cover_date or None,
        "description": description or None,
        "page_count": int(page_count) if page_count.isdigit() else None,
        "isbn_10": isbn_10 or None,
        "isbn_13": isbn_13 or None,
        "upc": upc or None,
        "comicvine_id": comicvine_id or None,
        "metron_id": metron_id or None,
        "cover_url_remote": cover_url_remote or None,
        "source": source or None,
        "source_id": source_id or None,
    }
    duplicate = await _find_duplicate(
        session,
        isbn_13=fields["isbn_13"],
        isbn_10=fields["isbn_10"],
        upc=fields["upc"],
        comicvine_id=fields["comicvine_id"],
        metron_id=fields["metron_id"],
    )
    if duplicate is not None:
        existing_copies = await _copy_count(session, duplicate.id)
        return templates.TemplateResponse(
            request,
            "partials/_duplicate.html",
            {"comic": duplicate, "copies": existing_copies, "fields": fields},
        )
    # Pre-fill the fandom picker with `star wars` when the candidate came
    # from Wookieepedia (we know it's SW). Otherwise leave empty.
    from app.services.fandoms import list_fandoms
    fandoms = await list_fandoms(session)
    current_fandom = "star wars" if fields["source"] == "wookieepedia" else None
    return templates.TemplateResponse(
        request, "partials/_confirm.html",
        {"fields": fields, "fandoms": fandoms, "current_fandom": current_fandom},
    )


@router.post("/add/save", response_class=HTMLResponse)
async def add_save(
    request: Request,
    session: SessionDep,
    background: BackgroundTasks,
    title: str = Form(""),
    series: str = Form(""),
    issue_number: str = Form(""),
    publisher: str = Form(""),
    cover_date: str = Form(""),
    description: str = Form(""),
    page_count: str = Form(""),
    isbn_10: str = Form(""),
    isbn_13: str = Form(""),
    upc: str = Form(""),
    comicvine_id: str = Form(""),
    metron_id: str = Form(""),
    cover_url_remote: str = Form(""),
    price_paid_eur: str = Form(""),
    existing_comic_id: str = Form(""),
    source: str = Form(""),
    source_id: str = Form(""),
    fandom: str = Form(""),
    fandom_new: str = Form(""),
) -> HTMLResponse:
    # Resolve the picker's two inputs: free-text wins over the dropdown,
    # `__NEW__` sentinel from the dropdown means "use fandom_new".
    from app.services.fandoms import normalize as _normalize_fandom
    if fandom == "__NEW__":
        fandom_chosen = _normalize_fandom(fandom_new)
    else:
        fandom_chosen = _normalize_fandom(fandom_new or fandom)
    if existing_comic_id.isdigit():
        comic = await session.get(Comic, int(existing_comic_id))
    else:
        comic = None

    if comic is None:
        publisher_row = await _get_or_create_publisher(session, publisher or None)
        # If we have a publisher but no explicit series (common with OL trades
        # where the volume name lives in the title), use the title as the
        # series name so the publisher actually attaches to the comic. The
        # user can rename the series from the detail page later.
        effective_series = series or (title if publisher else None)
        series_row = await _get_or_create_series(
            session, effective_series or None, publisher_row.id if publisher_row else None
        )
        comic = Comic(
            series_id=series_row.id if series_row else None,
            title=title or None,
            issue_number=issue_number or None,
            cover_date=_parse_date(cover_date),
            page_count=int(page_count) if page_count.isdigit() else None,
            isbn_10=isbn_10 or None,
            isbn_13=isbn_13 or None,
            upc=upc or None,
            comicvine_id=comicvine_id or None,
            metron_id=metron_id or None,
            cover_url_remote=cover_url_remote or None,
            description=description or None,
            source=source or None,
            source_id=source_id or None,
            # Fall back to "star wars" for Wookieepedia hits when the user
            # didn't explicitly choose anything in the picker. Other sources
            # leave it null and rely on the user (or the importer) to set it.
            fandom=fandom_chosen or ("star wars" if source == "wookieepedia" else None),
        )
        session.add(comic)
        await session.commit()
        await session.refresh(comic)
        if comic.cover_url_remote:
            background.add_task(_download_and_store_cover, comic.id, comic.cover_url_remote)

        # Pull rich data from the original cached candidate (creators, arcs,
        # extended metadata) and attach it to the freshly-saved comic.
        candidate = await _refetch_candidate(source, source_id)
        if candidate is not None:
            if candidate.creators:
                await _persist_creators(session, comic.id, candidate.creators)
            if candidate.story_arcs:
                await _persist_arcs(session, comic.id, candidate.story_arcs)
            await _backfill_metadata(session, comic, candidate)

        # Auto-tag based on source + Wookieepedia's canon flag.
        if source == "wookieepedia":
            await _ensure_tag(session, comic.id, "star wars")
            if candidate is not None and candidate.canon:
                await _ensure_tag(session, comic.id, candidate.canon)

        # Auto-tag from upstream metadata. Characters get a `chars: NAME`
        # prefix so they don't collide with free-form user tags; story arcs
        # and concepts go in bare. Capped per-bucket so a single CV issue
        # with 30 character credits doesn't drown the page.
        if candidate is not None:
            from app.routers.detail import _autotag_from_candidate
            await _autotag_from_candidate(session, comic.id, candidate)

    price = None
    try:
        price = float(price_paid_eur) if price_paid_eur else None
    except ValueError:
        price = None

    copy = Copy(comic_id=comic.id, price_paid_eur=price, purchase_date=datetime.now(UTC).date())
    session.add(copy)
    await session.commit()

    total_copies = await _copy_count(session, comic.id)
    return templates.TemplateResponse(
        request,
        "partials/_saved.html",
        {"comic": comic, "copies": total_copies, "series": series, "publisher": publisher},
    )


# ---------------------------------------------------------------------------
# Source refetch + creator/tag persistence helpers
# ---------------------------------------------------------------------------


async def _refetch_candidate(source: str, source_id: Optional[str]) -> Optional[LookupCandidate]:
    """Re-resolve the cached candidate the user picked, so we can pull rich
    fields (creators, etc.) the form didn't carry through."""
    if not source or not source_id:
        return None
    try:
        if source == "wookieepedia":
            return await wookieepedia.get_article(source_id)
        if source == "comicvine":
            return await comicvine.get_issue(source_id)
        if source == "metron":
            return await metron.get_issue(source_id)
    except Exception:
        return None
    return None


async def _persist_creators(session: AsyncSession, comic_id: int, creators) -> None:
    """Find-or-create Creator rows and link them via ComicCreator. Idempotent
    on (comic_id, creator_id, role) thanks to the composite primary key."""
    seen: set[tuple[int, str]] = set()
    for c in creators:
        name = (c.name or "").strip()
        role = (c.role or "").strip().lower() or "creator"
        if not name:
            continue
        result = await session.exec(select(Creator).where(Creator.name == name))
        creator_row = result.first()
        if creator_row is None:
            creator_row = Creator(name=name)
            session.add(creator_row)
            await session.flush()
        key = (creator_row.id, role)
        if key in seen:
            continue
        seen.add(key)
        link_result = await session.exec(
            select(ComicCreator).where(
                ComicCreator.comic_id == comic_id,
                ComicCreator.creator_id == creator_row.id,
                ComicCreator.role == role,
            )
        )
        if link_result.first() is None:
            session.add(ComicCreator(comic_id=comic_id, creator_id=creator_row.id, role=role))
    await session.commit()


async def _persist_arcs(session: AsyncSession, comic_id: int, arc_names) -> None:
    """Find-or-create StoryArc rows and link via ComicArc (idempotent)."""
    seen: set[int] = set()
    for raw in arc_names:
        name = re.sub(r"\s+", " ", raw or "").strip()
        if not name:
            continue
        result = await session.exec(select(StoryArc).where(StoryArc.name == name))
        arc = result.first()
        if arc is None:
            arc = StoryArc(name=name)
            session.add(arc)
            await session.flush()
        if arc.id in seen:
            continue
        seen.add(arc.id)
        link_result = await session.exec(
            select(ComicArc).where(
                ComicArc.comic_id == comic_id, ComicArc.arc_id == arc.id
            )
        )
        if link_result.first() is None:
            session.add(ComicArc(comic_id=comic_id, arc_id=arc.id))
    await session.commit()


async def _backfill_metadata(
    session: AsyncSession, comic: Comic, candidate: LookupCandidate,
    *, force: bool = False,
) -> None:
    """Copy cached candidate fields onto the saved Comic.

    Default behaviour (`force=False`, used at /add/save time) fills only
    columns the user left blank, so manual edits on the confirm form
    aren't overwritten.

    `force=True` (used by the refresh-from-source button) overwrites every
    source-derived column with the latest value from upstream — title,
    issue_number, cover, description, the lot. That's the whole point of
    refreshing.
    """
    from app.services.csv_import import translate_format as _norm_format
    from datetime import date as _date

    changed = False
    field_map = {
        "title": candidate.title,
        "issue_number": candidate.issue_number,
        "upc": candidate.upc,
        "collected_issues": candidate.collected_issues,
        # Normalize format to lowercase canonical form so the library
        # filter chips don't end up with both "Trade Paperback" and
        # "trade paperback" sitting side by side.
        "format": _norm_format(candidate.format),
        "language": candidate.language,
        "timeline": candidate.timeline,
        "era": candidate.era,
        "canon": candidate.canon,
        "page_count": candidate.page_count,
        "description": candidate.description,
        "cover_url_remote": candidate.cover_url,
    }
    # cover_date arrives as a string; coerce so the column accepts it.
    if candidate.cover_date:
        try:
            iso = candidate.cover_date[:10]
            field_map["cover_date"] = _date.fromisoformat(iso)
        except (TypeError, ValueError):
            pass

    # Source-only fields never surface on the confirm form, so the user
    # can't have a manual edit we'd be clobbering. Always write them
    # from the candidate, even when `force=False`. This was the cause of
    # the "save misses collected_issues; refresh fixes it" bug: a fresh
    # Comic has these blank → the `not getattr(...)` guard SHOULD pass,
    # but if any of these ever land non-null on the row (e.g. via CSV
    # import) the save flow would skip them silently. Forcing them keeps
    # save and refresh symmetric.
    SOURCE_ONLY = {"collected_issues", "format", "language", "timeline", "era", "canon"}

    for attr, value in field_map.items():
        if value is None:
            continue
        if force or attr in SOURCE_ONLY or not getattr(comic, attr):
            if getattr(comic, attr) != value:
                setattr(comic, attr, value)
                changed = True
                # When the remote cover URL changes, the cached local
                # file points at the OLD image. Drop it so the detail
                # page falls back to the new remote until the background
                # download lands a fresh local copy.
                if attr == "cover_url_remote":
                    comic.cover_url_local = None

    if changed:
        comic.updated_at = datetime.now(UTC)
        session.add(comic)
        await session.commit()


async def _ensure_tag(session: AsyncSession, comic_id: int, name: str) -> bool:
    """Find-or-create a Tag and link it to the comic. Returns True if a new
    link was created, False if the comic was already tagged or the name was
    blank after normalization."""
    name = re.sub(r"\s+", " ", name).strip().lower()
    if not name:
        return False
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
        return True
    await session.commit()
    return False
