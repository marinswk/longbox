"""Canon-comics master index.

Crawls Wookieepedia's `Category:Canon comic book issues` and
`Category:Canon trade paperbacks` category trees into the complete
set of canon single issues + trade paperbacks. The /missing pages
diff this against the user's library to show what isn't owned.

The crawl is slow (~250 category API calls) so it runs as a
background job with in-process progress; the result is cached as a
single MetadataCache row and survives restarts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlmodel import select

from app.db import SessionLocal
from app.models import Comic, MetadataCache
from app.services.collected_issues import coverage_titles, strip_disambiguator
from app.services.wookieepedia import list_category_members

_log = logging.getLogger("longbox.canon")

_ISSUES_ROOT = "Category:Canon comic book issues"
_ONESHOTS_ROOT = "Category:Canon one-shot comics"
_TPBS_ROOT = "Category:Canon trade paperbacks"
_CACHE_SOURCE = "canon_index"
_CACHE_KEY = "v1"

# Category-name suffixes stripped to recover the bare series name:
# "Star Wars: Darth Vader (2020) issues" -> "Star Wars: Darth Vader (2020)".
_SERIES_SUFFIXES = (
    " comic book issues", " issues", " trade paperbacks", " volumes",
)


def _series_from_category(cat_title: str) -> str:
    name = cat_title.replace("Category:", "").strip()
    # One-shot categories become a synthetic "<X> — One-shots" series.
    if name.lower().endswith(" one-shot comics"):
        return name[: -len(" one-shot comics")].strip() + " — One-shots"
    for suf in _SERIES_SUFFIXES:
        if name.endswith(suf):
            return name[: -len(suf)].strip()
    return name


def _natkey(s: str) -> list:
    """Natural sort key so "Vader 2" sorts before "Vader 10"."""
    return [
        int(p) if p.isdigit() else p.lower()
        for p in re.split(r"(\d+)", s)
    ]


# ---------------------------------------------------------------------------
# Background crawl
# ---------------------------------------------------------------------------


@dataclass
class CrawlProgress:
    running: bool = False
    phase: str = "Idle"
    done: int = 0
    total: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    issues_found: int = 0
    tpbs_found: int = 0
    error: str = ""

    @property
    def pct(self) -> int:
        return int(round(100 * self.done / self.total)) if self.total else 0

    @property
    def elapsed(self) -> int:
        end = self.finished_at or time.time()
        return int(end - self.started_at) if self.started_at else 0


_progress = CrawlProgress()
_lock = asyncio.Lock()
_task: asyncio.Task | None = None


def get_progress() -> CrawlProgress:
    return _progress


async def get_canon_index() -> dict | None:
    """Return the cached index dict, or None if it has never been built.

    Shape: ``{"built_at": iso, "issues": [[title, series], …],
    "tpbs": [[title, series], …]}``.
    """
    async with SessionLocal() as session:
        row = (await session.exec(
            select(MetadataCache).where(
                MetadataCache.source == _CACHE_SOURCE,
                MetadataCache.key == _CACHE_KEY,
            )
        )).first()
    if row is None:
        return None
    try:
        return json.loads(row.payload)
    except (ValueError, TypeError):
        return None


async def start_crawl() -> bool:
    """Start a crawl unless one is already running. Returns True if a
    new crawl was started."""
    global _progress, _task
    async with _lock:
        if _progress.running:
            return False
        _progress = CrawlProgress(
            running=True, phase="Starting…", started_at=time.time(),
        )
    _task = asyncio.create_task(_crawl())
    return True


async def _discover(root: str) -> list[str]:
    """Depth-first walk of a category tree → every category under
    `root` (root included)."""
    all_cats: list[str] = []
    seen: set[str] = set()
    stack = [root]
    while stack:
        cat = stack.pop()
        if cat in seen:
            continue
        seen.add(cat)
        all_cats.append(cat)
        for sub in await list_category_members(cat, member_type="subcat"):
            if sub not in seen:
                stack.append(sub)
    return all_cats


async def _collect(cats: list[str], root: str, fallback: str) -> dict[str, str]:
    """Fetch the article members of every category in `cats`. Returns
    `{page_title: series}` — the series is the page's own category
    (root direct-members get `fallback`). First series seen wins."""
    out: dict[str, str] = {}
    for cat in cats:
        series = fallback if cat == root else _series_from_category(cat)
        for page in await list_category_members(cat, member_type="page"):
            out.setdefault(page, series)
        _progress.done += 1
    return out


async def _crawl() -> None:
    p = _progress
    try:
        p.phase = "Discovering categories"
        issue_cats = await _discover(_ISSUES_ROOT)
        oneshot_cats = await _discover(_ONESHOTS_ROOT)
        tpb_cats = await _discover(_TPBS_ROOT)
        p.total = len(issue_cats) + len(oneshot_cats) + len(tpb_cats)
        p.done = 0

        p.phase = "Fetching canon single issues"
        issues = await _collect(issue_cats, _ISSUES_ROOT, "(uncategorised)")

        # One-shots are single comics too — fold them into the issue
        # list (they live in a separate Wookieepedia category tree).
        # An entry already classified as a numbered issue keeps that
        # series; a pure one-shot gets its "<X> — One-shots" group.
        p.phase = "Fetching canon one-shots"
        oneshots = await _collect(oneshot_cats, _ONESHOTS_ROOT, "Star Wars — One-shots")
        for title, series in oneshots.items():
            issues.setdefault(title, series)
        p.issues_found = len(issues)

        p.phase = "Fetching canon trade paperbacks"
        tpbs = await _collect(tpb_cats, _TPBS_ROOT, "Standalone")
        p.tpbs_found = len(tpbs)

        payload = {
            "built_at": datetime.now(UTC).isoformat(),
            "issues": sorted(([t, s] for t, s in issues.items())),
            "tpbs": sorted(([t, s] for t, s in tpbs.items())),
        }
        async with SessionLocal() as session:
            row = (await session.exec(
                select(MetadataCache).where(
                    MetadataCache.source == _CACHE_SOURCE,
                    MetadataCache.key == _CACHE_KEY,
                )
            )).first()
            now = datetime.now(UTC)
            if row is None:
                row = MetadataCache(
                    source=_CACHE_SOURCE, key=_CACHE_KEY,
                    payload=json.dumps(payload), fetched_at=now,
                )
            else:
                row.payload = json.dumps(payload)
                row.fetched_at = now
            session.add(row)
            await session.commit()
        p.phase = "Finished"
    except Exception as exc:  # noqa: BLE001 — never leave running stuck
        _log.exception("canon-index crawl failed")
        p.error = repr(exc)
        p.phase = "Failed"
    finally:
        p.running = False
        p.finished_at = time.time()


# ---------------------------------------------------------------------------
# Diff against the library
# ---------------------------------------------------------------------------


def _owned_coverage(comics: list[Comic]) -> set[str]:
    """Every issue/TPB title the library covers — each comic's own
    `source_id` plus every issue inside its `collected_issues`, with
    disambiguator-stripped variants folded in for redirect tolerance."""
    covered: set[str] = set()
    for c in comics:
        if c.source_id:
            covered.add(c.source_id)
        covered |= coverage_titles(c.collected_issues)
    covered |= {strip_disambiguator(t) for t in covered}
    return covered


def _group_missing(
    entries: list[list[str]], covered: set[str],
) -> dict:
    """Bucket `[title, series]` entries by series, splitting owned vs
    missing. Returns a summary dict the templates render directly."""
    groups: dict[str, dict] = {}
    total = owned = 0
    for title, series in entries:
        total += 1
        is_owned = title in covered or strip_disambiguator(title) in covered
        if is_owned:
            owned += 1
        g = groups.setdefault(series, {"series": series, "total": 0,
                                       "owned": 0, "missing": []})
        g["total"] += 1
        if is_owned:
            g["owned"] += 1
        else:
            g["missing"].append(title)
    out_groups = [g for g in groups.values() if g["missing"]]
    for g in out_groups:
        g["missing"].sort(key=_natkey)
    out_groups.sort(key=lambda g: g["series"].lower())
    return {
        "groups": out_groups,
        "total": total,
        "owned": owned,
        "missing": total - owned,
    }


def compute_missing(index: dict, comics: list[Comic]) -> dict:
    """Diff the canon index against the library. Returns
    `{"issues": <summary>, "tpbs": <summary>}`."""
    covered = _owned_coverage(comics)
    return {
        "issues": _group_missing(index.get("issues", []), covered),
        "tpbs": _group_missing(index.get("tpbs", []), covered),
    }
