"""Identifier-type detection and parallel multi-source lookup.

Identifier kinds and routing:

  ISBN-13 / ISBN-10  → Wookieepedia + Open Library in parallel.
                        Wookieepedia wins for Star Wars (richest data we have);
                        Open Library covers everything else.
  UPC                 → Metron + Wookieepedia in parallel.
                        Metron has a real `?upc=` filter on /api/issue/
                        that covers a lot of modern indie / Big Two
                        comics. Wookieepedia covers Star Wars (matched
                        via full-text search of the article wikitext).
                        Both 12-digit (series-level, multiple matches)
                        and 17–18-digit (issue-level + variant suffix)
                        UPCs are routed here. ComicVine doesn't index
                        UPCs at all and is intentionally skipped.
  Issue ID            → ComicVine + Metron in parallel — ComicVine first.

Per-source failures are swallowed so a flaky upstream can't break the whole
lookup. Rate-limit failures (`UpstreamRateLimit`) are reported separately
in the result so the picker can show a "served from cache, source X is
throttled" hint to the user. Result order reflects source priority, not
call latency.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import StrEnum

from app.services import comicvine, metron, openlibrary, wookieepedia
from app.services.errors import UpstreamRateLimit
from app.services.schemas import LookupCandidate

log = logging.getLogger(__name__)


class IdentifierKind(StrEnum):
    ISBN_10 = "isbn_10"
    ISBN_13 = "isbn_13"
    UPC = "upc"
    ISSUE_ID = "issue_id"


@dataclass
class LookupResult:
    """A successful lookup may still carry warnings — for instance a source
    hit its rate limit. The picker template renders the warnings as small
    chips above the candidates so the user knows why their library is
    smaller than usual."""

    candidates: list[LookupCandidate] = field(default_factory=list)
    rate_limited: list[str] = field(default_factory=list)


def _normalize(identifier: str) -> str:
    return identifier.replace("-", "").replace(" ", "").strip()


def detect(identifier: str) -> IdentifierKind:
    cleaned = _normalize(identifier)
    if not cleaned.isdigit():
        return IdentifierKind.ISSUE_ID
    n = len(cleaned)
    if n == 13 and cleaned.startswith(("978", "979")):
        return IdentifierKind.ISBN_13
    if n == 10:
        return IdentifierKind.ISBN_10
    if n == 12 or 13 <= n <= 18:
        return IdentifierKind.UPC
    return IdentifierKind.ISSUE_ID


@dataclass
class _SourceOutcome:
    """Either a list of candidates, a rate-limit notice, or nothing."""
    label: str
    candidates: list[LookupCandidate] = field(default_factory=list)
    rate_limited: bool = False


async def _safe(label: str, coro) -> _SourceOutcome:
    try:
        result = await coro
    except UpstreamRateLimit as exc:
        log.info("lookup source %s rate-limited: %s", label, exc.detail)
        return _SourceOutcome(label=label, rate_limited=True)
    except Exception:
        log.warning("lookup source %s failed", label, exc_info=True)
        return _SourceOutcome(label=label)
    return _SourceOutcome(label=label, candidates=_as_list(result))


def _as_list(result) -> list[LookupCandidate]:
    if result is None:
        return []
    if isinstance(result, list):
        return result
    return [result]


def _merge(*outcomes: _SourceOutcome) -> LookupResult:
    candidates: list[LookupCandidate] = []
    rate_limited: list[str] = []
    for o in outcomes:
        candidates.extend(o.candidates)
        if o.rate_limited:
            rate_limited.append(o.label)
    return LookupResult(candidates=candidates, rate_limited=rate_limited)


def _allowed(sources: set[str] | None, key: str) -> bool:
    """`sources=None` means 'no restriction' — all defaults run. Otherwise
    only the explicitly-listed source keys fire."""
    return sources is None or key in sources


async def lookup_full(identifier: str, *, sources: set[str] | None = None) -> LookupResult:
    """Full lookup result with both candidates and rate-limit warnings.

    `sources`, when given, restricts the fan-out to those source keys —
    the wizard passes the user's tile selection in so we don't make API
    calls (or surface rate-limit warnings) for sources they didn't pick.
    """
    ident = _normalize(identifier)
    kind = detect(ident)

    if kind in (IdentifierKind.ISBN_10, IdentifierKind.ISBN_13):
        coros = []
        if _allowed(sources, "wookieepedia"):
            coros.append(_safe("wookieepedia", wookieepedia.search_isbn(ident)))
        if _allowed(sources, "openlibrary"):
            coros.append(_safe("openlibrary", openlibrary.search_isbn(ident)))
        outcomes = await asyncio.gather(*coros) if coros else []
        return _merge(*outcomes)

    if kind is IdentifierKind.UPC:
        coros = []
        if _allowed(sources, "metron"):
            coros.append(_safe("metron", metron.search_upc(ident)))
        if _allowed(sources, "wookieepedia"):
            coros.append(_safe("wookieepedia", wookieepedia.search_upc(ident)))
        outcomes = await asyncio.gather(*coros) if coros else []
        return _merge(*outcomes)

    coros = []
    if _allowed(sources, "comicvine"):
        coros.append(_safe("comicvine", comicvine.get_issue(ident)))
    if _allowed(sources, "metron"):
        coros.append(_safe("metron", metron.get_issue(ident)))
    outcomes = await asyncio.gather(*coros) if coros else []
    return _merge(*outcomes)


async def lookup(identifier: str) -> list[LookupCandidate]:
    """Backwards-compatible candidates-only entry point. New callers should
    use `lookup_full` to also get rate-limit warnings."""
    result = await lookup_full(identifier)
    return result.candidates


async def search_text(query: str, *, sources: set[str] | None = None) -> LookupResult:
    """Free-text search across Wookieepedia + ComicVine + Metron.

    Each provider returns up to its own per-source cap (typically 20).
    Results are concatenated in source-priority order: Wookieepedia,
    then ComicVine, then Metron. Open Library is intentionally skipped
    for text search because its corpus is dominated by non-comic books.

    `sources`, when given, restricts the fan-out — see `lookup_full`.

    Per-source rate-limiting is captured in `LookupResult.rate_limited`
    so the picker can flag throttled sources to the user.
    """
    q = (query or "").strip()
    if not q:
        return LookupResult()

    coros = []
    if _allowed(sources, "wookieepedia"):
        coros.append(_safe("wookieepedia", wookieepedia.search_text(q)))
    if _allowed(sources, "comicvine"):
        coros.append(_safe("comicvine",   comicvine.search_text(q)))
    if _allowed(sources, "metron"):
        coros.append(_safe("metron",      metron.search_text(q)))
    outcomes = await asyncio.gather(*coros) if coros else []
    return _merge(*outcomes)


# ---------------------------------------------------------------------------
# Multi-field search (used by the CSV import wizard's per-row resolver)
# ---------------------------------------------------------------------------


_SOURCE_PRIORITY = {
    "wookieepedia": 0,
    "comicvine": 1,
    "metron": 2,
    "openlibrary": 3,
}


def _norm_tokens(s: str) -> set[str]:
    """Lowercase + alpha-tokenize a string for series-name overlap scoring."""
    import re as _re
    return set(_re.findall(r"[a-z0-9]+", (s or "").lower()))


def _candidate_year(c: LookupCandidate) -> int | None:
    """Best-guess publication year for ranking. Reads the cover_date string
    that every source populates (`YYYY-MM-DD` or `YYYY` formats both fly)."""
    cd = (c.cover_date or "").strip()
    if not cd:
        return None
    try:
        return int(cd[:4])
    except (TypeError, ValueError):
        return None


def _rank_score(
    cand: LookupCandidate,
    *,
    target_series: str | None,
    target_year: int | None,
    year_tolerance: int,
) -> tuple[int, int, int, int]:
    """Lower is better. Tuple ordering:
        0. year-distance penalty (∞ if outside tolerance, else |Δyears|)
        1. negative series-token-overlap (more overlap → smaller value)
        2. source priority (Wookieepedia first when applicable)
        3. negative cover-presence (cover URL ≈ richer hit, prefer them)
    """
    cand_year = _candidate_year(cand)
    if target_year is None or cand_year is None:
        year_pen = 0
    else:
        delta = abs(cand_year - target_year)
        year_pen = delta if delta <= year_tolerance else 1000 + delta

    overlap = 0
    if target_series:
        overlap = len(_norm_tokens(target_series) & _norm_tokens(cand.series or ""))

    src_prio = _SOURCE_PRIORITY.get(cand.source, 99)
    cover_bonus = 0 if cand.cover_url else 1
    return (year_pen, -overlap, src_prio, cover_bonus)


async def find_candidates_multi(
    *,
    series: str | None = None,
    title: str | None = None,
    year: int | None = None,
    issue_number: str | None = None,
    isbn: str | None = None,
    upc: str | None = None,
    sources: list[str] | None = None,
    year_tolerance: int = 1,
    limit: int = 5,
    custom_query: str | None = None,
) -> LookupResult:
    """Resolve a CSV-row's worth of fields against the metadata sources.

    Strategy:
      1. ISBN/UPC if present — fastest and exact, runs through `lookup_full`.
      2. Else series + title text search.
      3. Else fall back: title alone, then series alone.
    Then filter to the user's chosen `sources` list (None = no filter) and
    rank by year-proximity + series-name token overlap + source priority.
    Returns up to `limit` candidates, deduped by `(source, source_id)`.
    """
    chosen = set(sources) if sources else None
    rate_limited: list[str] = []
    candidates: list[LookupCandidate] = []

    # 0. User-supplied freeform query — used by the import wizard's
    #    per-row "search again with my own text" box. Bypasses the
    #    series/title/ISBN derivation entirely.
    if custom_query:
        q = custom_query.strip()
        if q:
            r = await search_text(q, sources=chosen)
            candidates.extend(r.candidates)
            rate_limited.extend(r.rate_limited)

    # 1. ISBN / UPC exact-match path. Pass `chosen` so we don't even hit
    #    sources the user deselected (saves a request and avoids
    #    surfacing irrelevant rate-limit warnings).
    if (isbn or upc) and not candidates:
        ident = (isbn or upc or "").strip()
        if ident:
            r = await lookup_full(ident, sources=chosen)
            candidates.extend(r.candidates)
            rate_limited.extend(r.rate_limited)

    # 2/3. Text search if nothing useful came back from ISBN/UPC.
    if not candidates:
        queries: list[str] = []
        if series and title:
            queries.append(f"{series} {title}".strip())
        if title:
            queries.append(title.strip())
        if series:
            queries.append(series.strip())
        # De-duplicate while preserving order.
        seen_q: set[str] = set()
        ordered: list[str] = []
        for q in queries:
            if q and q not in seen_q:
                seen_q.add(q)
                ordered.append(q)

        for q in ordered:
            r = await search_text(q, sources=chosen)
            candidates.extend(r.candidates)
            rate_limited.extend(r.rate_limited)
            if candidates:
                break  # stop at the first query that produced anything

    # Filter the candidate pool to the chosen sources too. Belt-and-braces:
    # `lookup_full` / `search_text` already respect `sources`, but a
    # mocked aggregator (or a future source that doesn't honor the flag)
    # shouldn't be able to leak into the result.
    if chosen is not None:
        candidates = [c for c in candidates if c.source in chosen]
        # Same filter applies to the rate-limit warnings — only surface
        # throttling for sources the user actually asked us to use.
        rate_limited = [s for s in rate_limited if s in chosen]

    # Dedup by (source, source_id) keeping first-seen.
    deduped: list[LookupCandidate] = []
    seen: set[tuple[str, str | None]] = set()
    for c in candidates:
        key = (c.source, c.source_id)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    # Rank.
    deduped.sort(key=lambda c: _rank_score(
        c, target_series=series, target_year=year, year_tolerance=year_tolerance,
    ))

    return LookupResult(
        candidates=deduped[:limit],
        rate_limited=list(dict.fromkeys(rate_limited)),
    )
