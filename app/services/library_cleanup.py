"""Library-wide cleanup job.

One button on /series kicks this off: a single background task that

  1. re-refreshes every Series' `expected_issues` (+ canceled list)
     from its upstream source,
  2. re-runs collected-issues → series inference for every Comic so
     multi-series links pick up parser improvements, then
  3. prunes dangling links and empty inference-artefact series.

Progress is held in a module-global `CleanupProgress` and polled by
the UI over HTMX. The app is a single-process container, so in-memory
state is sufficient — a server restart just abandons the run (every
step is idempotent, so re-running is always safe).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from sqlmodel import select

from app.db import SessionLocal
from app.models import Comic, ComicSeries, Series
from app.services import comicvine, metron, wookieepedia

_log = logging.getLogger("longbox.cleanup")

# source name -> async fetcher(source_id) -> list[str] of issue labels.
_FETCHERS = {
    "wookieepedia": wookieepedia.get_series_issues,
    "comicvine": comicvine.get_volume_issues,
    "metron": metron.get_series_issues,
}

# Keep the error log bounded — a pathological run shouldn't grow it
# without limit.
_MAX_ERRORS = 50


@dataclass
class CleanupProgress:
    """Live state of the cleanup run, shared with the polling UI."""
    running: bool = False
    phase: str = "Idle"
    phase_index: int = 0          # 1..4 while running, 0 idle
    phase_count: int = 4
    done: int = 0                 # items finished in the current phase
    total: int = 0                # items in the current phase
    started_at: float = 0.0
    finished_at: float = 0.0
    # Tallies, accumulated across the whole run.
    series_refreshed: int = 0
    series_skipped: int = 0
    series_failed: int = 0
    comics_repulled: int = 0
    comics_relinked: int = 0
    comics_failed: int = 0
    series_merged: int = 0
    primaries_reassigned: int = 0
    series_pruned: int = 0
    links_pruned: int = 0
    mislinks_removed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def pct(self) -> int:
        """Completion of the CURRENT phase, 0-100."""
        if not self.total:
            return 100 if not self.running else 0
        return int(round(100 * self.done / self.total))

    @property
    def elapsed(self) -> int:
        end = self.finished_at or time.time()
        return int(end - self.started_at) if self.started_at else 0

    def _note(self, msg: str) -> None:
        if len(self.errors) < _MAX_ERRORS:
            self.errors.append(msg)


_progress = CleanupProgress()
_lock = asyncio.Lock()
_task: asyncio.Task | None = None


def get_progress() -> CleanupProgress:
    """Current (or last-finished) cleanup state."""
    return _progress


async def start_cleanup() -> bool:
    """Start a cleanup run unless one is already in flight.

    Returns True if a new run was started, False if one was already
    running (the caller should just show the existing progress).
    """
    global _progress, _task
    async with _lock:
        if _progress.running:
            return False
        _progress = CleanupProgress(
            running=True, phase="Starting…", started_at=time.time(),
        )
    _task = asyncio.create_task(_run())
    return True


async def _refresh_one_series(series_id: int) -> None:
    """Re-pull one series' expected-issue list from its upstream.

    Series without a usable source are counted as skipped. An empty
    upstream result leaves the existing list untouched (so a
    transient miss can't wipe good data)."""
    async with SessionLocal() as session:
        series = await session.get(Series, series_id)
        if series is None:
            return
        src = (series.source or "").strip().lower()
        sid = (series.source_id or "").strip()
        name = series.name

    # Wookieepedia source_ids ARE article titles, so the series name
    # is a sound fallback when the id wasn't stored. CV/Metron need a
    # numeric id we can't guess — skip those.
    if src == "wookieepedia" and not sid:
        sid = name
    if not src or not sid:
        _progress.series_skipped += 1
        return

    fetcher = _FETCHERS.get(src)
    if fetcher is None:
        _progress.series_skipped += 1
        return

    try:
        issues = await fetcher(sid)
    except Exception as exc:  # rate limit, network, parse — all non-fatal
        _progress.series_failed += 1
        _progress._note(f"{name}: refresh failed ({exc!r})")
        return

    if not issues:
        # Don't destroy a previously-good list on a transient miss.
        _progress.series_skipped += 1
        return

    canceled: list[str] = []
    if src == "wookieepedia":
        try:
            canceled = await wookieepedia.get_series_canceled_issues(sid)
        except Exception:
            canceled = []

    async with SessionLocal() as session:
        series = await session.get(Series, series_id)
        if series is None:
            return
        series.source = src
        series.source_id = sid
        series.expected_issues = "\n".join(issues)
        series.canceled_issues = "\n".join(canceled) if canceled else None
        session.add(series)
        await session.commit()
    _progress.series_refreshed += 1


async def _repull_one_comic(comic_id: int) -> None:
    """Re-pull one comic from its upstream source.

    A comic with a source + source_id goes through `apply_repick` —
    the canonical force-overwrite pipeline that re-fetches the
    candidate, rewrites every source-owned field (collected_issues,
    format, canon, era, cover…), reassigns its series and re-runs
    multi-series inference. Re-parsing the (cached) upstream wikitext
    with the current parser is what propagates every parser fix to
    legacy rows.

    A comic with no usable source can't be re-pulled — it falls back
    to local inference over its existing `collected_issues` so its
    series links still pick up parser improvements.
    """
    from fastapi import BackgroundTasks

    from app.routers.add import _attach_inferred_series
    from app.services.repick import apply_repick

    async with SessionLocal() as session:
        comic = await session.get(Comic, comic_id)
        if comic is None:
            return
        src = (comic.source or "").strip()
        sid = (comic.source_id or "").strip()
        if src and sid:
            bg = BackgroundTasks()
            try:
                outcome = await apply_repick(
                    session, comic, source=src, source_id=sid, background=bg,
                )
            except Exception as exc:
                _progress.comics_failed += 1
                _progress._note(f"comic {comic_id}: re-pull crashed ({exc!r})")
                return
            if outcome.ok:
                _progress.comics_repulled += 1
                # Run the queued cover download(s) inline.
                try:
                    await bg()
                except Exception:
                    pass
                return
            _progress.comics_failed += 1
            _progress._note(f"comic {comic_id}: {outcome.message}")
            return

    # No source to re-pull from — best-effort local re-inference.
    try:
        await _attach_inferred_series(comic_id)
        _progress.comics_relinked += 1
    except Exception as exc:
        _progress.comics_failed += 1
        _progress._note(f"comic {comic_id}: inference failed ({exc!r})")


def _expected_set(series: Series) -> frozenset[str]:
    return frozenset(
        ln.strip()
        for ln in (series.expected_issues or "").splitlines()
        if ln.strip()
    )


async def _merge_subsumed_series() -> int:
    """Collapse every "subsumed" series into the smallest series that
    fully contains it.

    A series A is subsumed by B when A's expected-issue set is a
    strict subset of B's — every issue A tracks is already tracked by
    B, so the standalone A row is pure duplication. This is what
    consolidates a scattered family like the *Classic Star Wars*
    sub-series (Han Solo at Stars' End, The Empire Strikes Back, …)
    back into the single *Classic Star Wars* umbrella the user wants.

    The merge target is the SMALLEST strict superset (the most
    specific umbrella); if that target is itself subsumed, the chain
    is followed to its root so nothing merges into a row that's about
    to disappear. Returns the number of series merged away.
    """
    from app.services.series_merge import merge_series

    async with SessionLocal() as session:
        rows = (await session.exec(select(Series))).all()
    sets: dict[int, frozenset[str]] = {}
    sourced: set[int] = set()
    for s in rows:
        exp = _expected_set(s)
        if exp:
            sets[s.id] = exp
        if (s.source or "").strip():
            sourced.add(s.id)

    ids = list(sets)
    # Each subsumed series -> its smallest strict superset. A series
    # backed by a real upstream source is never merged into a
    # sourceless one — that would dissolve a genuine Wookieepedia
    # series into a synthetic umbrella (see _synthesize_umbrella_series).
    target_of: dict[int, int] = {}
    for a in ids:
        supers = [
            b for b in ids
            if b != a and sets[a] < sets[b]
            and not (a in sourced and b not in sourced)
        ]
        if supers:
            target_of[a] = min(supers, key=lambda b: len(sets[b]))

    merged = 0
    for source_id, target_id in target_of.items():
        # Follow the chain to a non-subsumed root so we never merge
        # into a series that is itself about to be merged away.
        seen: set[int] = {source_id}
        root = target_id
        while root in target_of and root not in seen:
            seen.add(root)
            root = target_of[root]
        if root == source_id:
            continue
        try:
            async with SessionLocal() as session:
                if await merge_series(session, source_id, root):
                    merged += 1
        except Exception as exc:
            _progress._note(f"merge {source_id}->{root} failed ({exc!r})")
    return merged


async def _reassign_franchise_primaries() -> int:
    """Re-home comics off franchise / artefact primary series.

    A trade whose own Wookieepedia article names only a broad
    franchise ("Star Wars: The High Republic", "Star Wars Rebels")
    gets that franchise as its primary Series — an empty, sourceless
    row that can never track anything. For each such comic, move its
    primary to the dominant REAL series among its links: the linked
    series (with a known issue list) whose issues overlap the comic's
    collected content the most. The emptied franchise rows are then
    pruned by the orphan sweep. Returns the number reassigned.
    """
    from app.services.collected_issues import (
        coverage_titles, strip_disambiguator,
    )

    async with SessionLocal() as session:
        rows = (await session.exec(select(Series))).all()
    franchise_ids = {
        s.id for s in rows
        if not (s.expected_issues or "").strip()
        and not (s.source or "").strip()
    }
    if not franchise_ids:
        return 0
    expected_by_id: dict[int, frozenset[str]] = {}
    for s in rows:
        exp = _expected_set(s)
        if exp:
            expected_by_id[s.id] = exp

    reassigned = 0
    async with SessionLocal() as session:
        for fid in franchise_ids:
            comic_ids = [
                r if isinstance(r, int) else r[0]
                for r in (await session.exec(
                    select(Comic.id).where(Comic.series_id == fid)
                )).all()
            ]
            for cid in comic_ids:
                comic = await session.get(Comic, cid)
                if comic is None:
                    continue
                cov = coverage_titles(comic.collected_issues)
                cov |= {strip_disambiguator(t) for t in cov}
                if comic.source_id:
                    cov.add(comic.source_id)
                    cov.add(strip_disambiguator(comic.source_id))
                linked = {
                    r if isinstance(r, int) else r[0]
                    for r in (await session.exec(
                        select(ComicSeries.series_id)
                        .where(ComicSeries.comic_id == cid)
                    )).all()
                }
                best: int | None = None
                best_overlap = 0
                for sid in linked:
                    if sid == fid:
                        continue
                    exp = expected_by_id.get(sid)
                    if not exp:
                        continue
                    overlap = len(cov & exp)
                    if overlap > best_overlap:
                        best_overlap = overlap
                        best = sid
                if best is None:
                    continue
                comic.series_id = best
                session.add(comic)
                reassigned += 1
        await session.commit()
    return reassigned


async def _prune_mislinked_series() -> int:
    """Remove non-primary ComicSeries links a comic has no business in.

    A link is "mis-linked" when the series HAS a known expected-issue
    list and NONE of the comic's collected issues (or its own
    source_id) appear in it. Such links are stale artefacts of earlier
    parser bugs — e.g. the en-dash one-shot title
    "War of the Bounty Hunters – Jabba the Hutt 1" being mis-split and
    its "Jabba the Hutt 1" half resolved to the unrelated 1995
    *Jabba the Hutt* series.

    Conservative on purpose: a series with no expected_issues is left
    alone (can't judge it), the primary link is never touched, and any
    real issue/story overlap keeps the link. With whole-library trade
    matching, dropping a link never hurts progress anyway — it only
    removes a wrong chip / a wrong entry on the series page.
    """
    from app.services.collected_issues import (
        coverage_titles, strip_disambiguator,
    )
    from sqlalchemy import delete as sa_delete

    async with SessionLocal() as session:
        series_rows = (await session.exec(select(Series))).all()
        link_rows = (await session.exec(
            select(ComicSeries).where(ComicSeries.is_primary == False)  # noqa: E712
        )).all()

    # Judge-able series: those with a non-empty expected list. Index
    # each title under its disambiguator-stripped form too.
    expected_by_series: dict[int, set[str]] = {}
    for s in series_rows:
        exp: set[str] = set()
        for ln in (s.expected_issues or "").splitlines():
            ln = ln.strip()
            if ln:
                exp.add(ln)
                exp.add(strip_disambiguator(ln))
        if exp:
            expected_by_series[s.id] = exp

    removed = 0
    async with SessionLocal() as session:
        for link in link_rows:
            exp = expected_by_series.get(link.series_id)
            if exp is None:
                continue  # series has no expected list — can't judge
            comic = await session.get(Comic, link.comic_id)
            if comic is None or comic.series_id == link.series_id:
                continue
            cov = coverage_titles(comic.collected_issues)
            cov |= {strip_disambiguator(t) for t in cov}
            if comic.source_id:
                cov.add(comic.source_id)
                cov.add(strip_disambiguator(comic.source_id))
            if cov & exp:
                continue  # justified — comic really covers this series
            await session.exec(
                sa_delete(ComicSeries).where(
                    ComicSeries.comic_id == link.comic_id,
                    ComicSeries.series_id == link.series_id,
                )
            )
            removed += 1
        await session.commit()
    return removed


async def _run() -> None:
    """The cleanup body. Each item is isolated in its own try/except so
    one bad series or comic can't abort the whole run."""
    p = _progress
    try:
        # ---- Phase 1: refresh every series from upstream ----------
        p.phase_index = 1
        p.phase = "Refreshing series from upstream"
        async with SessionLocal() as session:
            rows = (await session.exec(select(Series.id))).all()
        series_ids = [r if isinstance(r, int) else r[0] for r in rows]
        p.total = len(series_ids)
        p.done = 0
        for sid in series_ids:
            try:
                await _refresh_one_series(sid)
            except Exception as exc:  # defensive — _refresh handles its own
                p.series_failed += 1
                p._note(f"series {sid}: {exc!r}")
            p.done += 1

        # ---- Phase 2: re-pull every comic from upstream -----------
        # apply_repick re-fetches each comic, force-overwrites its
        # source-owned fields (collected_issues, format, canon, era,
        # cover…) and re-runs series inference — so every parser fix
        # reaches legacy rows. Comics with no source fall back to
        # local re-inference.
        p.phase_index = 2
        p.phase = "Re-pulling comics from upstream"
        async with SessionLocal() as session:
            rows = (await session.exec(select(Comic.id))).all()
        comic_ids = [r if isinstance(r, int) else r[0] for r in rows]
        p.total = len(comic_ids)
        p.done = 0
        for cid in comic_ids:
            try:
                await _repull_one_comic(cid)
            except Exception as exc:  # defensive — _repull handles its own
                p.comics_failed += 1
                p._note(f"comic {cid}: {exc!r}")
            p.done += 1

        # ---- Phase 3: consolidate series ---------------------------
        # Merge subsumed sub-series into their umbrella, then re-home
        # comics off empty franchise/artefact primary series onto the
        # real series their content belongs to.
        p.phase_index = 3
        p.phase = "Consolidating series"
        p.total = 1
        p.done = 0
        try:
            p.series_merged = await _merge_subsumed_series()
        except Exception as exc:
            p._note(f"subsumed-merge failed ({exc!r})")
        try:
            p.primaries_reassigned = await _reassign_franchise_primaries()
        except Exception as exc:
            p._note(f"primary-reassign failed ({exc!r})")
        p.done = 1

        # ---- Phase 4: prune orphans + mis-links -------------------
        p.phase_index = 4
        p.phase = "Pruning orphans & mis-links"
        p.total = 1
        p.done = 0
        from app.services.fandoms import (
            backfill_comic_series_links,
            backfill_prune_dangling_comicseries,
            backfill_prune_empty_inferred_series,
        )
        try:
            # Normalise the link table after the reassignments above
            # so is_primary flags match each Comic.series_id.
            await backfill_comic_series_links()
            p.links_pruned = await backfill_prune_dangling_comicseries()
            p.mislinks_removed = await _prune_mislinked_series()
            p.series_pruned = await backfill_prune_empty_inferred_series()
        except Exception as exc:
            p._note(f"prune failed ({exc!r})")
        p.done = 1
        p.phase = "Finished"
    except Exception as exc:  # last-ditch — never leave running=True stuck
        _log.exception("library cleanup crashed")
        p._note(f"cleanup crashed: {exc!r}")
        p.phase = "Failed"
    finally:
        p.running = False
        p.finished_at = time.time()
