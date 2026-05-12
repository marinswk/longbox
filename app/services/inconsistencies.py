"""Read-only sweep that flags comics whose stored data shape disagrees
with itself — usually the result of a wrong-pick during the import wizard.

The sweep is deliberately *liberal* about flagging: each row is a
suggestion, not an automatic fix. The user reviews via the re-pick UI
(`/comic/{id}/repick`) one at a time. False positives are cheap; missed
cases are not.

Heuristics (all run in one pass over the comics table):

  prose_collects
    `collected_issues` matches "COLLECTING:" prose or contains a comma.
    Likely the user's CSV said it's a TPB but the import grabbed the
    single-issue article — the prose stuck around as the only marker.

  format_collects_mismatch
    `collected_issues` is set AND `format` isn't a trade-ish binding.
    Single issues, digitals, etc. don't legitimately collect anything.

  single_issue_pattern_with_trade_format
    `format` is a trade-ish binding AND `source_id` looks like a
    single-issue Wookieepedia article ("The High Republic 1") rather
    than a TPB ("…Vol. 1"). The most common wrong-pick after import.

  cover_date_year_mismatch
    `cover_date.year` is far from the median of the rest of this
    series's comics. Picks up cases where one issue got linked to a
    sibling series with a totally different decade. Skipped for tiny
    series (need ≥ 5 dated comics to compute a useful median).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import Comic, Series

_TRADE_FORMATS = {"trade paperback", "hardcover", "omnibus", "graphic novel"}

# Article titles that include "Vol", "Volume", "Collection", "Omnibus",
# "TPB", "Hardcover" are clearly *not* single-issue articles.
_TRADE_MARKERS_RE = re.compile(
    r"\b(vol|volume|collection|omnibus|tpb|hardcover)\b", re.IGNORECASE,
)
_TRAILING_NUM_RE = re.compile(r" \d{1,3}$")
_COLLECTING_PROSE_RE = re.compile(r"^\s*collect(?:s|ing)\b\s*:?", re.IGNORECASE)


@dataclass
class Reason:
    code: str
    label: str
    severity: str  # "info" | "warn" | "error"


@dataclass
class FlaggedComic:
    comic: Comic
    series: Optional[Series]
    reasons: list[Reason]


def _looks_like_single_issue_article(source_id: Optional[str]) -> bool:
    if not source_id:
        return False
    if _TRADE_MARKERS_RE.search(source_id):
        return False
    return bool(_TRAILING_NUM_RE.search(source_id))


async def find_inconsistencies(
    session: AsyncSession, *, year_tolerance: int = 5,
) -> list[FlaggedComic]:
    """One pass over the comics table; returns flagged rows in `Comic.id`
    order. `year_tolerance` is in years for the `cover_date_year_mismatch`
    heuristic — kept generous because legitimate series can span decades."""
    rows = (await session.exec(
        select(Comic, Series).join(
            Series, Series.id == Comic.series_id, isouter=True,
        ).order_by(Comic.id.asc())
    )).all()

    # Pre-compute median cover-year per series (for the year heuristic).
    series_years: dict[int, list[int]] = defaultdict(list)
    for c, _s in rows:
        if c.series_id and c.cover_date:
            series_years[c.series_id].append(c.cover_date.year)
    series_median: dict[int, int] = {}
    for sid, yrs in series_years.items():
        if len(yrs) >= 5:
            yrs_sorted = sorted(yrs)
            series_median[sid] = yrs_sorted[len(yrs_sorted) // 2]

    flagged: list[FlaggedComic] = []
    for comic, series in rows:
        reasons: list[Reason] = []
        ci = (comic.collected_issues or "").strip()
        fmt = (comic.format or "").strip().lower()

        # 1. prose_collects
        if ci and (_COLLECTING_PROSE_RE.match(ci) or "," in ci):
            reasons.append(Reason(
                code="prose_collects",
                label="Collected issues looks like prose / a list, not "
                      "per-line article titles.",
                severity="warn",
            ))

        # 2. format_collects_mismatch
        if ci and fmt and fmt not in _TRADE_FORMATS:
            reasons.append(Reason(
                code="format_collects_mismatch",
                label=(
                    f"Has collected_issues but format is "
                    f"“{fmt}” (not a trade)."
                ),
                severity="warn",
            ))

        # 3. single_issue_pattern_with_trade_format
        if fmt in _TRADE_FORMATS and _looks_like_single_issue_article(comic.source_id):
            reasons.append(Reason(
                code="single_issue_pattern_with_trade_format",
                label=(
                    f"Format=“{fmt}” but source_id looks like a "
                    f"single-issue article ({comic.source_id})."
                ),
                severity="error",
            ))

        # 4. cover_date_year_mismatch
        if (comic.series_id in series_median
                and comic.cover_date is not None):
            delta = abs(comic.cover_date.year - series_median[comic.series_id])
            if delta > year_tolerance:
                reasons.append(Reason(
                    code="cover_date_year_mismatch",
                    label=(
                        f"Cover date {comic.cover_date.year} is {delta}y "
                        f"off this series' median "
                        f"({series_median[comic.series_id]})."
                    ),
                    severity="info",
                ))

        if reasons:
            flagged.append(FlaggedComic(comic=comic, series=series, reasons=reasons))

    return flagged
