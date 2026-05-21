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
    phase_index: int = 0          # 1..3 while running, 0 idle
    phase_count: int = 3
    done: int = 0                 # items finished in the current phase
    total: int = 0                # items in the current phase
    started_at: float = 0.0
    finished_at: float = 0.0
    # Tallies, accumulated across the whole run.
    series_refreshed: int = 0
    series_skipped: int = 0
    series_failed: int = 0
    comics_processed: int = 0
    links_added: int = 0
    series_pruned: int = 0
    links_pruned: int = 0
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


async def _count_links(comic_id: int) -> int:
    async with SessionLocal() as session:
        rows = (await session.exec(
            select(ComicSeries.series_id).where(ComicSeries.comic_id == comic_id)
        )).all()
    return len({r if isinstance(r, int) else r[0] for r in rows})


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

        # ---- Phase 2: re-infer comic -> series links --------------
        p.phase_index = 2
        p.phase = "Re-linking comics to series"
        from app.routers.add import _attach_inferred_series
        async with SessionLocal() as session:
            rows = (await session.exec(
                select(Comic.id)
                .where(Comic.collected_issues.is_not(None))
                .where(Comic.collected_issues != "")
            )).all()
        comic_ids = [r if isinstance(r, int) else r[0] for r in rows]
        p.total = len(comic_ids)
        p.done = 0
        for cid in comic_ids:
            try:
                before = await _count_links(cid)
                await _attach_inferred_series(cid)
                after = await _count_links(cid)
                p.links_added += max(0, after - before)
                p.comics_processed += 1
            except Exception as exc:
                p._note(f"comic {cid}: inference failed ({exc!r})")
            p.done += 1

        # ---- Phase 3: prune dangling links + empty series ---------
        p.phase_index = 3
        p.phase = "Pruning orphans"
        p.total = 1
        p.done = 0
        from app.services.fandoms import (
            backfill_prune_dangling_comicseries,
            backfill_prune_empty_inferred_series,
        )
        try:
            p.links_pruned = await backfill_prune_dangling_comicseries()
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
