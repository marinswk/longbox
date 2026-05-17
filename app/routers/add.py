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


async def _attach_inferred_series(comic_id: int) -> None:
    """Auto-attach a Comic to every singles-series implied by its
    `collected_issues` list. Idempotent — skips series the comic is
    already linked to (via either the primary FK or the link table).

    Resolution strategy per inferred series group:
      1. Fetch the SAMPLE issue article (e.g. "Knights of the Old
         Republic 1") from Wookieepedia.
      2. Read its `series=` infobox to get the canonical series-
         article title (e.g. "Star Wars: Knights of the Old Republic
         (comic series)"). That's what `wookieepedia.get_article`
         returns as `candidate.series` after our hierarchical-bullet
         parsing.
      3. Find-or-create the Series by that canonical name. Stamp it
         with `source = wookieepedia` + the article title as source_id
         so /series/{id}/auto-link can pull the issue list directly
         without further guessing.

    Fallback: when the issue article doesn't exist on Wookieepedia,
    or the upstream lookup fails for any reason, we degrade to the
    name-only behaviour (find-or-create by the trailing-number-
    stripped guess). Better than skipping — the link still gets
    made; the auto-link page can be used to fix the source_id later.

    Series rows created here inherit the primary series' publisher_id
    so the library publisher facet doesn't sprout stub-pubs.

    Skipped entirely if `collected_issues` is empty (singles, one-
    shots) or if none of the entries match the `<Series> <Number>`
    shape.
    """
    from app.models import ComicSeries
    from app.services.collected_issues import derive_inferred_series
    from app.services import wookieepedia

    # PHASE 1 — read-only DB session: load comic state + snapshot
    # existing links + figure out which publisher to inherit. The
    # session is CLOSED before any HTTP call so the cache writes
    # those calls trigger can't deadlock against this transaction
    # (SQLite is single-writer; holding a session open while
    # downstream code opens a second session for the cache layer is
    # the easiest way to provoke a "database is locked" error, which
    # in turn made the inferrer silently fall back to guess names).
    async with SessionLocal() as session:
        comic = await session.get(Comic, comic_id)
        if comic is None:
            return
        raw_collected = comic.collected_issues
        primary_series_id = comic.series_id
        publisher_id = None
        if primary_series_id is not None:
            primary = await session.get(Series, primary_series_id)
            if primary is not None:
                publisher_id = primary.publisher_id
        existing_link_ids = {
            r if isinstance(r, int) else r[0]
            for r in (await session.exec(
                select(ComicSeries.series_id)
                .where(ComicSeries.comic_id == comic_id)
            )).all()
        }

    # PHASE 2 — outside any DB session: resolve EVERY linkable
    # collected-issues entry via Wookieepedia and group the results
    # by their canonical series. Resolving only one sample per
    # trailing-number-stripped guess (the old behaviour) silently
    # mis-routed cases like "The Old Republic 1/2/3" + "The Old
    # Republic 4/5/6" — both have guess "The Old Republic" but they
    # belong to two completely different series articles. Grouping
    # by canonical-after-resolution catches these split-name cases.
    import asyncio as _asyncio
    import logging as _lg
    from app.services.collected_issues import parse_entries
    from app.services.schemas import LookupCandidate
    entries = parse_entries(raw_collected)
    # Pass EVERY linkable entry through — not just ones ending with a
    # number. One-shots like "Jabba the Hutt: The Gaar Suppoon Hit"
    # don't have a trailing number but their issue articles DO
    # carry a `series=` infobox value we should follow. When an
    # entry has `article_id` set (em-dash combined StoryCite entries
    # like "Story — Pizzazz 1"), use that — the display text is just
    # a label.
    linkable_titles = [
        (e.article_id or e.text) for e in entries if e.linkable
    ]
    if not linkable_titles:
        return

    # Bound concurrency so a 70-issue omnibus doesn't smack
    # Wookieepedia with 70 parallel requests. The first save pays
    # the network cost; subsequent saves hit MetadataCache.
    sem = _asyncio.Semaphore(8)

    async def _resolve(t):
        async with sem:
            # Step 1: direct resolution — the issue article's
            # series= infobox usually points at the canonical
            # series.
            for _attempt in range(2):
                try:
                    cand = await wookieepedia.get_article(t)
                    if cand is not None and cand.series:
                        return t, cand
                except Exception as e:
                    _lg.getLogger("longbox.infer").warning(
                        "resolution failed for %r: %r", t, e,
                    )
                    cand = None
            # Step 2: fallback — when the infobox series= is empty
            # (some Dark Horse miniseries articles leave it blank,
            # e.g. "Darth Vader and the Ninth Assassin 1"), derive
            # the series from the issue title by stripping the
            # trailing issue number and trying both bare + the
            # canonical "Star Wars: " prefixed form. We use
            # `get_series_issues` as the existence probe rather than
            # `get_article` because series-page articles use the
            # `ComicSeries` infobox template, which `get_article`
            # doesn't recognise (it gates on ComicBook /
            # ComicCollection). If the variant article exists AND
            # has a parseable Issues section, treat it as the
            # canonical series.
            m = re.match(r"^(.+?)\s+\d+[A-Za-z]?$", t)
            if m:
                guess_base = m.group(1).strip()
                for variant in (f"Star Wars: {guess_base}", guess_base):
                    try:
                        probe_issues = await wookieepedia.get_series_issues(variant)
                    except Exception:
                        probe_issues = []
                    if probe_issues:
                        return t, LookupCandidate(
                            source="wookieepedia",
                            source_id=t,
                            title=t,
                            series=variant,
                        )
            return t, None

    resolved_pairs = await _asyncio.gather(
        *( _resolve(t) for t in linkable_titles )
    )

    # Group by canonical name. Keep one representative title per
    # group so we can derive a name_guess (used by the rename/merge
    # branches in Phase 3).
    by_canon: dict[str, dict] = {}
    for title, cand in resolved_pairs:
        if cand is None or not cand.series:
            continue
        key = cand.series
        if key in by_canon:
            continue
        guess_match = re.match(r"^(.+?)\s+\d+[A-Za-z]?$", title)
        guess = guess_match.group(1).strip() if guess_match else title
        by_canon[key] = {
            "name_guess": guess,
            "sample_issue_title": title,
            "article_id": cand.series_article_id or cand.series,
        }
    if not by_canon:
        return

    # Pre-fetch expected_issues per canonical (cached after first
    # hit, same Semaphore-bounded parallelism). Also pull the
    # canceled-issues subset so newly-created Series rows get the
    # correct progress denominator from the start.
    async def _series_issues(article_id):
        async with sem:
            try:
                return article_id, await wookieepedia.get_series_issues(article_id)
            except Exception:
                return article_id, []
    async def _series_canceled(article_id):
        async with sem:
            try:
                return article_id, await wookieepedia.get_series_canceled_issues(article_id)
            except Exception:
                return article_id, []
    issue_results = await _asyncio.gather(
        *( _series_issues(info["article_id"]) for info in by_canon.values() )
    )
    canceled_results = await _asyncio.gather(
        *( _series_canceled(info["article_id"]) for info in by_canon.values() )
    )
    issues_by_article: dict[str, list[str]] = dict(issue_results)
    canceled_by_article: dict[str, list[str]] = dict(canceled_results)

    # Drop "subsumed" canonicals: when sub-series A's issue list is a
    # strict subset of umbrella B's, the user already gets full
    # coverage tracking through B and the standalone A row is just
    # noise (e.g. "Dark Times—Out of the Wilderness 1-5" is included
    # in the broader "Dark Times" series' 33-issue list). Skipping A
    # cleans up the cluttered SERIES section on the comic detail
    # page without losing any tracking info.
    canon_list = list(by_canon.keys())
    issue_sets = {
        canon: set(issues_by_article.get(by_canon[canon]["article_id"], []))
        for canon in canon_list
    }
    subsumed: set[str] = set()
    for a in canon_list:
        a_set = issue_sets[a]
        if not a_set:
            continue
        for b in canon_list:
            if a == b:
                continue
            b_set = issue_sets[b]
            if a_set < b_set:  # strict subset
                subsumed.add(a)
                break
    for canon in subsumed:
        by_canon.pop(canon, None)

    resolved: list[tuple] = []
    for canonical_name, info in by_canon.items():
        # Build a minimal pseudo-group object for the Phase-3 logic.
        class _G:
            name_guess = info["name_guess"]
            sample_issue_title = info["sample_issue_title"]
        resolved.append((
            _G,
            canonical_name,
            info["article_id"],
            issues_by_article.get(info["article_id"], []),
            canceled_by_article.get(info["article_id"], []),
        ))

    # PHASE 3 — fresh DB session for the writes. Now the cache reads
    # / writes from Phase 2 are done; we can hold this session as
    # long as we like.
    async with SessionLocal() as session:
        for group, canonical_name, canonical_article, issues, canceled in resolved:
            # Look up by canonical name first. If absent AND the
            # canonical differs from the guess, also check the guess —
            # an earlier name-only inference may have created a row
            # under that name; we rename it to the canonical instead
            # of creating a duplicate. If BOTH rows exist (rare —
            # means the inferrer was run with different code at
            # different times), merge by reassigning the guess's
            # links onto the canonical and deleting the guess row.
            existing = (await session.exec(
                select(Series).where(Series.name == canonical_name)
            )).first()
            guess_row = None
            if canonical_name != group.name_guess:
                guess_row = (await session.exec(
                    select(Series).where(Series.name == group.name_guess)
                )).first()
            # Don't auto-create a brand-new Series row when the
            # canonical article yields no issues — it would just
            # show up as a useless 0/0 entry in the library. Both
            # existing-row paths (rename guess → canonical, update
            # canonical) still run so we don't drop useful state.
            if existing is None and guess_row is None and not issues:
                continue

            if existing is None and guess_row is not None:
                # Rename the guess row in place. Carries over all the
                # existing ComicSeries / Comic.series_id references
                # without touching them.
                guess_row.name = canonical_name
                if canonical_article and not guess_row.source_id:
                    guess_row.source = "wookieepedia"
                    guess_row.source_id = canonical_article
                session.add(guess_row)
                await session.flush()
                existing = guess_row
            elif existing is not None and guess_row is not None and guess_row.id != existing.id:
                # Both rows exist — collapse the guess into the canonical.
                # Done via raw SQL because SQLAlchemy ORM doesn't allow
                # updating an instance's primary key columns cleanly
                # (the (comic_id, series_id) composite would change),
                # and we may already have rows at the destination PK.
                from sqlalchemy import update as sa_update, delete as sa_delete, text
                from app.models import ComicSeries as _CS
                # Snapshot ids before we drop the ORM-managed instance.
                # Accessing `.id` after a `session.delete()` triggers a
                # lazy-load outside the greenlet context and explodes.
                gid = guess_row.id
                eid = existing.id
                # 1. Reassign every Comic.series_id pointer.
                await session.exec(
                    sa_update(Comic)
                    .where(Comic.series_id == gid)
                    .values(series_id=eid)
                )
                # 2. Copy guess link rows into canonical rows. INSERT
                #    OR IGNORE handles the case where a comic is
                #    already linked to both (would-be PK collision).
                await session.exec(text(
                    "INSERT OR IGNORE INTO comicseries "
                    "  (comic_id, series_id, is_primary, created_at) "
                    "SELECT comic_id, :canonical, is_primary, created_at "
                    "FROM comicseries WHERE series_id = :guess"
                ).bindparams(canonical=eid, guess=gid))
                # 3. Drop all guess-pointed link rows + the guess row.
                await session.exec(
                    sa_delete(_CS).where(_CS.series_id == gid)
                )
                await session.exec(
                    sa_delete(Series).where(Series.id == gid)
                )
                await session.commit()

            if existing is None:
                existing = Series(
                    name=canonical_name,
                    publisher_id=publisher_id,
                    source="wookieepedia" if canonical_article else None,
                    source_id=canonical_article,
                    expected_issues="\n".join(issues) if issues else None,
                    canceled_issues="\n".join(canceled) if canceled else None,
                )
                session.add(existing)
                await session.flush()
            else:
                # Stamp source/source_id on a previously-bare row that
                # was originally created by name-only inference.
                dirty = False
                if canonical_article and not existing.source_id:
                    existing.source = "wookieepedia"
                    existing.source_id = canonical_article
                    dirty = True
                # Pre-populate expected_issues for rows that don't
                # have one yet. We never overwrite an existing list —
                # the user may have edited it manually.
                if issues and not existing.expected_issues:
                    existing.expected_issues = "\n".join(issues)
                    dirty = True
                if canceled and not existing.canceled_issues:
                    existing.canceled_issues = "\n".join(canceled)
                    dirty = True
                if dirty:
                    session.add(existing)

            if existing.id == primary_series_id:
                continue
            if existing.id in existing_link_ids:
                continue
            # Final defensive check — the merge branch above may have
            # just inserted this link by copying it from the guess
            # row, and our cached `existing_link_ids` set wouldn't
            # know. Re-checking the DB avoids tripping the UNIQUE
            # constraint on (comic_id, series_id).
            already = (await session.exec(
                select(ComicSeries).where(
                    ComicSeries.comic_id == comic_id,
                    ComicSeries.series_id == existing.id,
                )
            )).first()
            if already is not None:
                existing_link_ids.add(existing.id)
                continue
            session.add(ComicSeries(
                comic_id=comic_id,
                series_id=existing.id,
                is_primary=False,
            ))
            existing_link_ids.add(existing.id)
        await session.commit()


async def _enrich_series_from_candidate(
    series_id: int, source: str, candidate_series: Optional[str],
    candidate_raw: Optional[dict],
    candidate_series_article_id: Optional[str] = None,
) -> None:
    """Best-effort background fill of `Series.source` / `Series.source_id`
    / `Series.expected_issues` using the same upstream the comic came from.

    Without this, /add/save creates a bare Series row (just name +
    publisher_id), leaving the series detail page without a missing-
    issues list until the user manually pastes a source_id into the
    /series/{id}/refresh form. Doing it here matches what the user
    expects when they save a single-issue article into a new series.

    Source rules:
      - wookieepedia: `candidate.series` IS an article title most of the
        time. Try `get_series_issues(title)`; it parses the wiki article
        and returns the issue list (empty for trade/anthology pages,
        which is fine — we just skip the write).
      - comicvine: extract `volume.id` from the raw issue payload and
        feed it to `get_volume_issues`.
      - metron: extract `series.id` from the raw issue payload.

    Idempotent: if the Series already has a source AND expected_issues
    we leave it alone — refresh is the right tool for explicit updates.
    """
    if not source or not candidate_series:
        return
    async with SessionLocal() as session:
        series = await session.get(Series, series_id)
        if series is None:
            return
        # Already enriched — let the user use /series/{id}/refresh if
        # they want to update.
        if series.source and series.expected_issues:
            return

        fetcher = None
        source_id: Optional[str] = None
        if source == "wookieepedia":
            fetcher = wookieepedia.get_series_issues
            # Prefer the explicit series_article_id when set (e.g. Epic
            # Collection sub-imprints encoded as "Epic Collection#Legends");
            # fall back to the bare series name otherwise.
            source_id = candidate_series_article_id or candidate_series
        elif source == "comicvine":
            raw = candidate_raw or {}
            vol = raw.get("volume") or {}
            vid = vol.get("id")
            if vid is not None:
                from app.services import comicvine as _cv
                fetcher = _cv.get_volume_issues
                source_id = str(vid)
        elif source == "metron":
            raw = candidate_raw or {}
            ser = raw.get("series") or {}
            sid = ser.get("id")
            if sid is not None:
                from app.services import metron as _metron
                fetcher = _metron.get_series_issues
                source_id = str(sid)

        if fetcher is None or not source_id:
            return
        try:
            issues = await fetcher(source_id)
        except Exception:
            # Best-effort: any upstream hiccup leaves the Series in its
            # bare-bones state. The /series/{id}/refresh form is the
            # explicit fallback.
            return
        if not issues:
            return

        series.source = source
        series.source_id = source_id
        # Don't clobber a manually-entered expected_issues list.
        if not series.expected_issues:
            series.expected_issues = "\n".join(issues)
        # Pull the canceled-issues subset too (Wookieepedia only).
        # Same conservatism: keep an existing canceled_issues list.
        if source == "wookieepedia" and not series.canceled_issues:
            try:
                canceled = await wookieepedia.get_series_canceled_issues(source_id)
            except Exception:
                canceled = []
            if canceled:
                series.canceled_issues = "\n".join(canceled)
        session.add(series)
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
        # Multi-series link gets written after the Comic is committed
        # below. The `series_id` FK + the ComicSeries row stay in sync
        # via that post-commit step.
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
        # Mirror the primary series link into the multi-series link
        # table. The Comic's `series_id` stays as the "primary" pointer
        # for backward-compat queries; ComicSeries is the source of
        # truth for membership-aware views. Guard against duplicate
        # inserts — the lifespan backfill may have already created
        # this row (it runs on every cold start across the shared
        # test DB, and an end user re-saving the same comic shouldn't
        # crash either).
        if comic.series_id is not None:
            from app.models import ComicSeries
            already = (await session.exec(
                select(ComicSeries).where(
                    ComicSeries.comic_id == comic.id,
                    ComicSeries.series_id == comic.series_id,
                )
            )).first()
            if already is None:
                session.add(ComicSeries(
                    comic_id=comic.id, series_id=comic.series_id, is_primary=True,
                ))
                await session.commit()
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

        # Best-effort series enrichment. Fires only for newly-created
        # series rows where we haven't pulled the upstream issue list
        # yet — this is what makes /series/{id} render a missing-issues
        # progress bar after a fresh save, instead of requiring the user
        # to manually visit the page and paste source/source_id.
        if candidate is not None and comic.series_id is not None:
            background.add_task(
                _enrich_series_from_candidate,
                comic.series_id,
                source,
                candidate.series,
                candidate.raw,
                candidate.series_article_id,
            )

        # Auto-attach to every underlying singles series implied by
        # the collected_issues list. For omnibuses / TPBs this is
        # what makes them appear on each contained series' detail
        # page automatically, without the user typing names into the
        # multi-series form. No-op for singles (empty collected_issues).
        background.add_task(_attach_inferred_series, comic.id)

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
