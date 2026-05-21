"""Issue-level duplicate detection across single issues, TPBs and
omnibuses.

The user's library is a mix of three formats — single-issue comics,
trade paperbacks (collections of issues), and omnibuses (bigger
collections). The same underlying issue (identified by its
Wookieepedia article title) can appear through any of those paths
simultaneously:

  * Single issue: `Comic.source_id` is the article title.
  * TPB / Omnibus: every linkable entry in `collected_issues`
    points at an underlying issue article (the `article_id` field
    on `CollectedEntry` covers paren-combined StoryCite entries).

This module builds a reverse index `issue_title → [owning Comics]`
and returns the entries with two or more owners. A "derived series"
is attached per duplicate so the UI can group the results.

Pure data — no DB queries, no HTML. Easy to unit-test.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable, Optional

from app.models import Comic, Series
from app.services.collected_issues import coverage_titles, strip_disambiguator


@dataclass
class DuplicateOwner:
    """One Comic that covers a duplicated issue. The format string is
    a normalised label ("single issue", "trade paperback",
    "hardcover", "omnibus", …) drawn from `Comic.format`. The icon
    is a small visual cue the template renders alongside."""
    comic: Comic
    format_label: str
    format_icon: str  # 📕 (single/trade) / 📚 (hardcover/omnibus) / 📖 fallback


@dataclass
class DuplicateRow:
    issue_title: str        # Wookieepedia article title
    derived_series: str     # bucket for grouping (Series.name or guess)
    owners: list[DuplicateOwner]

    @property
    def count(self) -> int:
        return len(self.owners)

    @property
    def has_single(self) -> bool:
        return any(o.format_label == "single issue" for o in self.owners)

    @property
    def has_collection(self) -> bool:
        return any(
            o.format_label in ("trade paperback", "hardcover", "omnibus")
            for o in self.owners
        )


def _format_icon(fmt: Optional[str]) -> str:
    if not fmt:
        return "📖"
    f = fmt.lower()
    if f in ("hardcover", "omnibus"):
        return "📚"
    if f in ("trade paperback", "single issue"):
        return "📕"
    return "📖"


def _comic_coverage(comic: Comic, known_issues: set[str]) -> set[str]:
    """The set of Wookieepedia issue article titles this comic
    covers — its own source_id if it's a SINGLE-ISSUE
    Wookieepedia-sourced comic, plus every linkable
    `collected_issues` entry that we recognise as a real issue
    article.

    `known_issues` is the union of every series'
    `expected_issues` — our ground-truth of "what's an actual
    issue article on Wookieepedia". Filtering against it strips
    out collected-content noise like short-story titles ("Old
    Wounds", "The Taris Holofeed: Prime Edition") that appear in
    omnibus/TPB contents alongside the issues but aren't issue
    articles themselves. Without this filter, two omnibuses that
    both list a short story by name would show up as a "duplicate"
    even though there's no real issue-level overlap to act on.

    TPB / omnibus Comics don't contribute their `source_id`
    because that's the COLLECTION's article title, not an issue.
    """
    coverage: set[str] = set()
    fmt = (comic.format or "").lower()
    if (
        comic.source == "wookieepedia"
        and comic.source_id
        and fmt == "single issue"
        and comic.source_id in known_issues
    ):
        coverage.add(comic.source_id)
    # `coverage_titles` yields the story title, the host-book title and
    # the verbatim line for combined StoryCite entries — so a trade
    # that reprints an anthology one-shot's story is recognised by the
    # story name (the key Wookieepedia files under each series). A
    # story may be referenced via a disambiguated redirect title, so
    # fall back to the disambiguator-stripped form against the
    # ground-truth issue set.
    for article in coverage_titles(comic.collected_issues):
        if article in known_issues:
            coverage.add(article)
            continue
        norm = strip_disambiguator(article)
        if norm != article and norm in known_issues:
            coverage.add(norm)
    return coverage


def _derive_series(
    issue_title: str, series_by_issue: dict[str, list[Series]],
) -> str:
    """Map an issue article title to the best grouping label.

    Prefer the SMALLEST series (most specific) when multiple series
    cover the issue — e.g. "Knights of the Old Republic: War 1" is
    in both "Knights of the Old Republic" (52 issues) and the
    smaller "Knights of the Old Republic: War" (5 issues); we
    group under the latter.

    Falls back to a trailing-number-strip of the issue title when
    no series covers it (e.g. orphan singles).
    """
    candidates = series_by_issue.get(issue_title, [])
    if candidates:
        # Smallest expected_issues = most specific bucket.
        best = min(
            candidates,
            key=lambda s: len((s.expected_issues or "").splitlines()),
        )
        return best.name
    # Fallback heuristic.
    import re as _re
    m = _re.match(r"^(.+?)\s+\d+[A-Za-z]?$", issue_title)
    return m.group(1).strip() if m else issue_title


def build_duplicate_index(
    comics: Iterable[Comic],
    series: Iterable[Series],
    *,
    min_copies: int = 2,
) -> list[DuplicateRow]:
    """Walk owned comics, build the issue→owners reverse index,
    and return rows where the issue appears in `min_copies` or more
    distinct owning comics.

    `comics` should be pre-filtered to OWNED (Copy count > 0) —
    this function doesn't open a DB session itself.

    `series` is used to derive the most-specific series label per
    duplicate. Pass every Series; the function indexes by the
    intersection of expected_issues sets.
    """
    # Pre-build issue → [series] index from expected_issues lists.
    # `known_issues` doubles as the "real issue article" filter for
    # comic coverage so we don't count short-story titles or other
    # collected-content noise as duplicates.
    series_by_issue: dict[str, list[Series]] = defaultdict(list)
    known_issues: set[str] = set()
    for s in series:
        for line in (s.expected_issues or "").splitlines():
            line = line.strip()
            if line:
                series_by_issue[line].append(s)
                known_issues.add(line)

    # Walk comics, build the reverse index.
    issue_to_owners: dict[str, list[DuplicateOwner]] = defaultdict(list)
    for comic in comics:
        coverage = _comic_coverage(comic, known_issues)
        if not coverage:
            continue
        owner = DuplicateOwner(
            comic=comic,
            format_label=(comic.format or "").lower() or "unknown",
            format_icon=_format_icon(comic.format),
        )
        for issue in coverage:
            issue_to_owners[issue].append(owner)

    # Filter + build rows.
    rows: list[DuplicateRow] = []
    for issue, owners in issue_to_owners.items():
        if len(owners) < min_copies:
            continue
        rows.append(DuplicateRow(
            issue_title=issue,
            derived_series=_derive_series(issue, series_by_issue),
            owners=sorted(
                owners,
                key=lambda o: (
                    o.format_label,
                    (o.comic.title or "").lower(),
                ),
            ),
        ))
    return rows


def apply_filters_and_sort(
    rows: list[DuplicateRow], *, mix: str = "all", sort: str = "count_desc",
    series: Optional[str] = None,
) -> list[DuplicateRow]:
    """Filter then sort. Pure helper so the router stays thin."""
    out = rows
    if mix == "singles_and_collection":
        out = [r for r in out if r.has_single and r.has_collection]
    elif mix == "collections_only":
        out = [r for r in out if r.has_collection and not r.has_single]
    if series:
        out = [r for r in out if r.derived_series == series]

    if sort == "title_asc":
        out = sorted(out, key=lambda r: r.issue_title.lower())
    elif sort == "series_asc":
        out = sorted(out, key=lambda r: (
            r.derived_series.lower(), -r.count, r.issue_title.lower(),
        ))
    else:  # count_desc (default)
        out = sorted(out, key=lambda r: (
            -r.count, r.derived_series.lower(), r.issue_title.lower(),
        ))
    return out


def group_by_series(rows: list[DuplicateRow]) -> list[dict]:
    """Bucket rows by `derived_series` for the grouped template
    render. Returns `[{label, count, rows}, ...]` sorted by total
    duplicates per group descending."""
    buckets: dict[str, list[DuplicateRow]] = defaultdict(list)
    for r in rows:
        buckets[r.derived_series].append(r)
    out = [
        {"label": label, "rows": rs, "count": sum(r.count - 1 for r in rs)}
        for label, rs in buckets.items()
    ]
    out.sort(key=lambda g: (-g["count"], g["label"].lower()))
    return out


def stats(rows: list[DuplicateRow]) -> dict:
    """Top-of-page summary: total duplicated issues, total
    "wasted" copies (sum of count-1), top-3 most-duplicated
    series by wasted-copy count."""
    total_issues = len(rows)
    extra_copies = sum(r.count - 1 for r in rows)
    by_series: dict[str, int] = defaultdict(int)
    for r in rows:
        by_series[r.derived_series] += r.count - 1
    top_series = sorted(by_series.items(), key=lambda kv: -kv[1])[:3]
    triple_plus = sum(1 for r in rows if r.count >= 3)
    return {
        "total_issues": total_issues,
        "extra_copies": extra_copies,
        "top_series": top_series,
        "triple_plus": triple_plus,
    }
