"""Stats page.

Computes everything in one request as a set of aggregate queries against
the existing tables. No new schema. Charts are rendered client-side from
JSON data embedded in the template.

Sections (top to bottom):
  1. KPI strip                — comics / copies / series / publishers
  2. Composition donuts       — format · canon · era
  3. Physical-copies donuts   — read status · condition · storage
  4. Activity bars            — added per month · read per month
  5. Series progress + highlights
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.db import get_session
from app.models import Comic, ComicTag, Copy, Series
from app.services.series_progress import compute_progress

router = APIRouter(tags=["stats"])

APP_DIR = Path(__file__).resolve().parents[1]
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def _scalar(result, default=0):
    val = result.first()
    if val is None:
        return default
    if isinstance(val, tuple):
        val = val[0]
    return val if val is not None else default


async def _column_distribution(
    session: AsyncSession, column, *, unset_label: str = "unset"
) -> tuple[list[dict], int]:
    """Group-by-and-count helper for a single column. Returns
    `(rows, populated_count)` where rows are `[{"label": str, "count": int}, ...]`
    sorted by descending count, and populated_count is how many rows had a
    non-null value (used for the data-availability gates and the per-chart
    coverage caption)."""
    raw = (
        await session.exec(select(column, func.count()).group_by(column))
    ).all()
    out: list[dict] = []
    populated = 0
    for value, n in raw:
        label = value if value not in (None, "") else unset_label
        if value not in (None, ""):
            populated += int(n or 0)
        out.append({"label": label, "count": int(n or 0)})
    out.sort(key=lambda r: -r["count"])
    return out, populated


async def _gather(session: AsyncSession) -> dict:
    now = datetime.now(UTC)

    # --- Section 1: KPI strip ----------------------------------------
    total_comics = int(_scalar(await session.exec(select(func.count(Comic.id)))) or 0)
    total_copies = int(_scalar(await session.exec(select(func.count(Copy.id)))) or 0)
    # Count every Series row — the same population the /series page
    # and the "series progress" section below report. Counting only
    # distinct `Comic.series_id` (the old query) ignored series a
    # comic belongs to via the multi-series link table, so the KPI
    # badly undercounted (e.g. 31 vs the real 139).
    total_series = int(_scalar(
        await session.exec(select(func.count(Series.id)))
    ) or 0)
    total_publishers = int(_scalar(
        await session.exec(
            select(func.count(func.distinct(Series.publisher_id))).where(Series.publisher_id.is_not(None))
        )
    ) or 0)

    # --- Section 2: Composition (Comic-level) ------------------------
    formats, format_pop = await _column_distribution(session, Comic.format, unset_label="(unset)")
    canons,  canon_pop  = await _column_distribution(session, Comic.canon,  unset_label="unknown")
    eras,    era_pop    = await _column_distribution(session, Comic.era,    unset_label="(unset)")
    fandoms, fandom_pop = await _column_distribution(session, Comic.fandom, unset_label="(unset)")

    # --- Section 3: Physical copies (Copy-level) ---------------------
    read_rows = (
        await session.exec(select(Copy.read_status, func.count(Copy.id)).group_by(Copy.read_status))
    ).all()
    read_status = [{"status": (s or "unknown"), "count": int(n or 0)} for (s, n) in read_rows]

    cond_rows = (
        await session.exec(select(Copy.condition, func.count(Copy.id)).group_by(Copy.condition))
    ).all()
    conditions = [{"condition": (c or "unspecified"), "count": int(n or 0)} for (c, n) in cond_rows]

    storage, storage_pop = await _column_distribution(
        session, Copy.storage_location, unset_label="(unset)"
    )

    # --- Section 4: Activity over the last 12 months -----------------
    def _empty_month_buckets() -> "OrderedDict[str, int]":
        buckets: OrderedDict[str, int] = OrderedDict()
        for offset in range(11, -1, -1):
            anchor = now.replace(day=1) - timedelta(days=offset * 31)
            buckets[anchor.strftime("%Y-%m")] = 0
        return buckets

    cutoff = (now.replace(day=1) - timedelta(days=11 * 31)).replace(hour=0, minute=0, second=0)

    monthly_added = _empty_month_buckets()
    add_rows = (
        await session.exec(
            select(
                func.strftime("%Y-%m", Comic.created_at).label("ym"),
                func.count(Comic.id),
            )
            .where(Comic.created_at >= cutoff)
            .group_by("ym")
            .order_by("ym")
        )
    ).all()
    for ym, n in add_rows:
        if ym in monthly_added:
            monthly_added[ym] = int(n or 0)
    added_per_month = [{"month": k, "count": v} for k, v in monthly_added.items()]

    monthly_read = _empty_month_buckets()
    read_pace_rows = (
        await session.exec(
            select(
                func.strftime("%Y-%m", Copy.date_read).label("ym"),
                func.count(Copy.id),
            )
            .where(Copy.read_status == "read", Copy.date_read.is_not(None))
            .group_by("ym")
            .order_by("ym")
        )
    ).all()
    total_dated_reads = 0
    for ym, n in read_pace_rows:
        n = int(n or 0)
        total_dated_reads += n
        if ym in monthly_read:
            monthly_read[ym] = n
    read_per_month = [{"month": k, "count": v} for k, v in monthly_read.items()]

    # --- Section 5: Series progress aggregate + highlights -----------
    all_series = (await session.exec(select(Series))).all()
    progress = await compute_progress(session, [s.id for s in all_series])
    series_complete = sum(1 for p in progress.values() if p.is_complete)
    series_in_progress = sum(1 for p in progress.values() if not p.is_complete and p.total > 0)
    series_untracked = max(0, len(all_series) - series_complete - series_in_progress)

    # Read this year
    read_this_year = int(_scalar(
        await session.exec(
            select(func.count(Copy.id)).where(
                Copy.read_status == "read",
                func.strftime("%Y", Copy.date_read) == str(now.year),
            )
        )
    ) or 0)

    # Heaviest add-month (within the 12-month window) — Python-side scan.
    heaviest_month = None
    if any(b["count"] for b in added_per_month):
        top = max(added_per_month, key=lambda b: b["count"])
        if top["count"]:
            heaviest_month = {"month": top["month"], "count": top["count"]}

    # Oldest / most recent comics by cover_date.
    oldest = None
    oldest_row = (
        await session.exec(
            select(Comic.id, Comic.title, Comic.issue_number, Comic.cover_date)
            .where(Comic.cover_date.is_not(None))
            .order_by(Comic.cover_date.asc())
            .limit(1)
        )
    ).first()
    if oldest_row:
        cid, title, issue, cdate = oldest_row
        oldest = {"id": cid, "title": title, "issue_number": issue, "cover_date": str(cdate)}

    most_recent = None
    recent_row = (
        await session.exec(
            select(Comic.id, Comic.title, Comic.issue_number, Comic.cover_date)
            .where(Comic.cover_date.is_not(None))
            .order_by(Comic.cover_date.desc())
            .limit(1)
        )
    ).first()
    if recent_row:
        cid, title, issue, cdate = recent_row
        most_recent = {"id": cid, "title": title, "issue_number": issue, "cover_date": str(cdate)}

    # Most-tagged comic.
    most_tagged = None
    tag_count_col = func.count(ComicTag.tag_id).label("tags")
    tag_row = (
        await session.exec(
            select(Comic.id, Comic.title, Comic.issue_number, tag_count_col)
            .join(ComicTag, ComicTag.comic_id == Comic.id)
            .group_by(Comic.id)
            .order_by(tag_count_col.desc())
            .limit(1)
        )
    ).first()
    if tag_row:
        cid, title, issue, tcount = tag_row
        most_tagged = {
            "id": cid, "title": title, "issue_number": issue, "tag_count": int(tcount or 0),
        }

    return {
        "totals": {
            "comics": total_comics,
            "copies": total_copies,
            "series": total_series,
            "publishers": total_publishers,
        },
        # Composition
        "formats": formats,
        "canons": canons,
        "eras": eras,
        "fandoms": fandoms,
        # Physical copies
        "read_status": read_status,
        "conditions": conditions,
        "storage": storage,
        # Activity
        "added_per_month": added_per_month,
        "read_per_month": read_per_month,
        # Series + highlights
        "series_progress": {
            "complete": series_complete,
            "in_progress": series_in_progress,
            "untracked": series_untracked,
            "total": len(all_series),
        },
        "highlights": {
            "read_this_year": read_this_year,
            "heaviest_month": heaviest_month,
            "oldest": oldest,
            "most_recent": most_recent,
            "most_tagged": most_tagged,
        },
        # Coverage / data-availability flags so the template can hide
        # sections gracefully when the underlying column is empty.
        "present": {
            "format":   format_pop > 0,
            "canon":    canon_pop > 0,
            "era":      era_pop > 0,
            "fandom":   fandom_pop > 0,
            "storage":  storage_pop > 0,
            "read_pace": total_dated_reads > 0,
        },
    }


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, session: SessionDep) -> HTMLResponse:
    data = await _gather(session)
    return templates.TemplateResponse(request, "stats.html", {"data": data})
