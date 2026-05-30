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
    """Return the set of issue / story titles a comic's
    `collected_issues` blob can satisfy.

    Combined "Story (Book)" StoryCite entries contribute the story
    title, the book title AND the verbatim line — see
    `coverage_titles`. The story key is what lets a trade that
    collects an anthology one-shot's story (e.g. "Tool of the
    Empire", published inside "Revelations (2023) 1") count toward a
    series that lists the story itself as an issue."""
    from app.services.collected_issues import coverage_titles
    return coverage_titles(comic.collected_issues)


# Natural sort key so "Vader 2" sorts before "Vader 10" (a plain
# string sort would put "10" before "2"). Mirrors `_natkey` in
# `app/services/canon_index.py` — kept as a local copy to avoid a
# module dependency between the two. For a single series every entry
# shares the same name prefix (incl. any `(YYYY)` volume year), so the
# sort falls through to the trailing issue number; specials without a
# number ("Ashcan", "Annual 2018") sort after the numbered runs because
# their differing prefix text compares later.
def _natural_key(s: str) -> list:
    return [
        int(p) if p.isdigit() else p.lower()
        for p in re.split(r"(\d+)", s)
    ]


def parse_expected(series: Series) -> list[str]:
    """Return the expected issue list MINUS any titles flagged as
    canceled, **de-duplicated and naturally sorted**.

    Used by both the missing-issues detector + the progress
    denominator. Canceled issues are tracked separately on
    `Series.canceled_issues` (a sub-list of `expected_issues`) so they
    can still be SHOWN — just not counted against the user's completion
    percentage.

    De-duplication matters for correctness, not just looks: some
    Wookieepedia issue tables list the same issue twice (once per
    overlapping TPB grouping — e.g. *Star Wars Adventures* 14–18 sit
    in both Vol. 6 and Vol. 7 rows), which would otherwise inflate the
    denominator and double-count a trade match. Sorting fixes the
    common "issue 9 renders after issue 11" complaint where the
    upstream table is ordered by collection rather than issue number.
    """
    raw = series.expected_issues or ""
    cancelled_raw = series.canceled_issues or ""
    cancelled = {line.strip() for line in cancelled_raw.split("\n") if line.strip()}

    seen: set[str] = set()
    out: list[str] = []
    for line in raw.split("\n"):
        title = line.strip()
        if not title or title in cancelled or title in seen:
            continue
        seen.add(title)
        out.append(title)
    out.sort(key=_natural_key)
    return out


def parse_canceled(series: Series) -> list[str]:
    """Return the series' canceled-issue titles in the order they
    appear in `Series.canceled_issues`."""
    raw = series.canceled_issues or ""
    return [line.strip() for line in raw.split("\n") if line.strip()]


@dataclass
class MatchPair:
    title: str
    direct: Optional[Comic]
    trade: Optional[Comic]


def match_owned(
    expected: list[str],
    comics: list[Comic],
    trade_pool: list[Comic] | None = None,
) -> tuple[list[MatchPair], int]:
    """For each expected entry, find a Comic that satisfies it.

    Returns `(pairs, owned_count)`. Each pair has a title and at most one
    of `direct` (single-issue ownership) or `trade` (collected in a TPB).
    `owned_count` is the number of expected entries with any kind of
    match.

    `comics` are the comics linked to this series — used for the
    single-issue match paths (source_id, issue-number fallback), which
    only make sense scoped to the series.

    `trade_pool`, when given, is the WHOLE library: the collected-issues
    (trade) match is run against it, not just the linked comics. Issue
    article titles are globally unique, so a trade collecting
    "Darth Vader (2020) 12" genuinely covers that issue for ANY series
    that lists it — most importantly crossover/event series (e.g. War
    of the Bounty Hunters), whose tie-in issues are collected in the
    individual ongoing-series TPBs rather than under the event itself.
    """
    from app.services.collected_issues import strip_disambiguator

    by_source_id = {c.source_id: c for c in comics if c.source_id}
    by_issue_number: dict[str, list[Comic]] = {}
    for c in comics:
        if c.issue_number:
            by_issue_number.setdefault(c.issue_number, []).append(c)

    # Index every collected title under both its exact form AND its
    # disambiguator-stripped form, so a story collected via a
    # redirect title ("Tall Tales (Revelations)") still matches a
    # series that lists the canonical title ("Tall Tales") — and
    # vice versa. Linked comics are indexed FIRST so they win the
    # display attribution when both a linked and an unrelated trade
    # cover the same issue.
    trade_index: dict[str, Comic] = {}
    pools = [comics] if trade_pool is None else [comics, trade_pool]
    for pool in pools:
        for c in pool:
            for title in _collected_titles(c):
                trade_index.setdefault(title, c)
                norm = strip_disambiguator(title)
                if norm and norm != title:
                    trade_index.setdefault(norm, c)

    # Two-pass single-issue match: direct source_id wins, THEN
    # number-fallback fills remaining slots — but each Comic can only
    # satisfy ONE expected entry as a single. Without this, the
    # number fallback used to mark every "X 1" expected entry as
    # owned just because the user had one comic with issue_number=1
    # linked to the series. Trade (collected_issues) matches are
    # separate and CAN serve many expected entries from one trade.
    direct_by_index: dict[int, Comic] = {}
    consumed_ids: set[int] = set()

    # Pass A — direct source_id matches.
    for i, title in enumerate(expected):
        c = by_source_id.get(title)
        if c is None or c.id in consumed_ids:
            continue
        direct_by_index[i] = c
        consumed_ids.add(c.id)

    # Pass B — number-fallback for entries still without a direct
    # match. Picks the first comic with the matching issue_number
    # that hasn't already been consumed by Pass A.
    for i, title in enumerate(expected):
        if i in direct_by_index:
            continue
        num = _trailing_number(title)
        if num is None:
            continue
        for cand in by_issue_number.get(num, []):
            if cand.id in consumed_ids:
                continue
            direct_by_index[i] = cand
            consumed_ids.add(cand.id)
            break

    pairs: list[MatchPair] = []
    owned = 0
    for i, title in enumerate(expected):
        direct = direct_by_index.get(i)
        trade = None
        if direct is None:
            trade = trade_index.get(title)
            if trade is None:
                norm = strip_disambiguator(title)
                if norm != title:
                    trade = trade_index.get(norm)
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

    Comics are pulled via BOTH the primary `Comic.series_id` FK AND
    the multi-series `ComicSeries` link table. This matters for
    omnibuses / TPBs that collect issues from multiple underlying
    singles series: each underlying series owns the omnibus via a
    non-primary link, and the trade-match logic uses
    `comic.collected_issues` to mark every contained issue as owned.
    Without the multi-series query, /series/{id} for KotOR singles
    would show 0/52 even when the user owns the omnibus that
    collects them all.
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

    # Build a (series_id → list[Comic]) map by walking BOTH the
    # primary FK and the ComicSeries link table. We can't do this in
    # one bulk query without a UNION because the same comic might
    # legitimately be linked to multiple series — per-series dedup
    # via a set of (series_id, comic_id) tuples is the simplest path.
    from app.models import ComicSeries
    relevant_ids = [s.id for s in series_rows]
    comics_by_series: dict[int, dict[int, Comic]] = {sid: {} for sid in relevant_ids}

    # Path 1: primary FK matches.
    primary_rows = (
        await session.exec(
            select(Comic).where(Comic.series_id.in_(relevant_ids))
        )
    ).all()
    for c in primary_rows:
        comics_by_series[c.series_id][c.id] = c

    # Path 2: link-table matches.
    link_rows = (
        await session.exec(
            select(Comic, ComicSeries.series_id)
            .join(ComicSeries, ComicSeries.comic_id == Comic.id)
            .where(ComicSeries.series_id.in_(relevant_ids))
        )
    ).all()
    for c, sid in link_rows:
        comics_by_series[sid][c.id] = c

    # Whole-library pool for the trade match — lets a crossover/event
    # series count tie-in issues that are collected under the
    # individual ongoing series rather than under the event.
    trade_pool = (await session.exec(select(Comic))).all()

    out: dict[int, Progress] = {}
    for series in series_rows:
        expected = parse_expected(series)
        if not expected:
            continue
        comics = list(comics_by_series.get(series.id, {}).values())
        _pairs, owned = match_owned(expected, comics, trade_pool=trade_pool)
        out[series.id] = Progress(owned=owned, total=len(expected))
    return out
