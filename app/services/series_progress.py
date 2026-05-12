"""Series-completion matcher.

Given a Series with `expected_issues` populated and a list of owned
Comics for that series, decide which expected entries the user already
owns. Used by both:

  * the series detail page  (`/series/{id}`)  — full per-issue rendering
  * the library grid          — mini progress bar on each card

Three match paths, tried in order per expected entry:

  1. **Direct (single issue)**: `Comic.source_id == expected_title`
  2. **Number fallback**:        trailing digits of expected title
                                  ==  `Comic.issue_number`
  3. **Trade collection**:       any owned trade whose
                                  `collected_issues` contains the
                                  expected article title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import Comic, Series

# Number-at-end:  "Jedi Knights 1"   → "1"   (Wookieepedia article titles)
# `#N` prefix:    "#1 — Pilot"        → "1"   (ComicVine / Metron labels)
_TRAILING_NUM = re.compile(r"(\d+(?:\.\d+)?)\s*$")
_HASH_NUM = re.compile(r"#(\d+(?:\.\d+)?)")


def _trailing_number(label: str) -> Optional[str]:
    """Extract the issue number from a label, accepting either:
      * trailing digits  ("Jedi Knights 1")
      * a `#N` prefix    ("#1 — Pilot")

    Returns the digits as a string — matches `Comic.issue_number`, which
    is stored as text ("1", "0.5", "Annual 2", etc.).
    """
    m = _HASH_NUM.search(label)
    if m:
        return m.group(1)
    m = _TRAILING_NUM.search(label)
    return m.group(1) if m else None


def _collected_titles(comic: Comic) -> set[str]:
    raw = comic.collected_issues or ""
    return {line.strip() for line in raw.split("\n") if line.strip()}


def parse_expected(series: Series) -> list[str]:
    raw = series.expected_issues or ""
    return [line.strip() for line in raw.split("\n") if line.strip()]


@dataclass
class MatchPair:
    title: str
    direct: Optional[Comic]
    trade: Optional[Comic]


def match_owned(
    expected: list[str], comics: list[Comic]
) -> tuple[list[MatchPair], int]:
    """For each expected entry, find a Comic that satisfies it.

    Returns `(pairs, owned_count)`. Each pair has a title and at most one
    of `direct` (single-issue ownership) or `trade` (collected in a TPB).
    `owned_count` is the number of expected entries with any kind of
    match.
    """
    by_source_id = {c.source_id: c for c in comics if c.source_id}
    by_issue_number = {c.issue_number: c for c in comics if c.issue_number}

    trade_index: dict[str, Comic] = {}
    for c in comics:
        for title in _collected_titles(c):
            trade_index.setdefault(title, c)

    pairs: list[MatchPair] = []
    owned = 0
    for title in expected:
        direct = by_source_id.get(title)
        if direct is None:
            num = _trailing_number(title)
            if num is not None:
                direct = by_issue_number.get(num)
        trade = None if direct is not None else trade_index.get(title)
        if direct is not None or trade is not None:
            owned += 1
        pairs.append(MatchPair(title=title, direct=direct, trade=trade))
    return pairs, owned


@dataclass
class Progress:
    owned: int
    total: int

    @property
    def pct(self) -> int:
        if not self.total:
            return 0
        return int(round(100 * self.owned / self.total))

    @property
    def is_complete(self) -> bool:
        return self.total > 0 and self.owned >= self.total


async def compute_progress(
    session: AsyncSession, series_ids: list[int]
) -> dict[int, Progress]:
    """Bulk-compute completion progress for a set of series IDs.

    Skips series with no `expected_issues` (the upstream issue list
    hasn't been refreshed yet). Returns `{series_id: Progress}` for the
    series that DO have an issue list — callers can `dict.get()` and
    treat None as "no progress info available".
    """
    if not series_ids:
        return {}

    # Pull all relevant series with non-empty expected_issues in one query.
    series_rows = (
        await session.exec(
            select(Series).where(
                Series.id.in_(series_ids),
                Series.expected_issues.is_not(None),
            )
        )
    ).all()
    if not series_rows:
        return {}

    relevant_ids = [s.id for s in series_rows]

    # All comics for those series in one query, grouped per-series client-side.
    comics_rows = (
        await session.exec(
            select(Comic).where(Comic.series_id.in_(relevant_ids))
        )
    ).all()
    comics_by_series: dict[int, list[Comic]] = {}
    for c in comics_rows:
        comics_by_series.setdefault(c.series_id, []).append(c)

    out: dict[int, Progress] = {}
    for series in series_rows:
        expected = parse_expected(series)
        if not expected:
            continue
        comics = comics_by_series.get(series.id, [])
        _pairs, owned = match_owned(expected, comics)
        out[series.id] = Progress(owned=owned, total=len(expected))
    return out
