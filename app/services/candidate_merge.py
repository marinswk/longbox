"""Merge book-source lookup candidates that describe the same ISBN.

Open Library and Google Books both answer an ISBN scan, but with
complementary gaps — OL often has a cover but no description; Google
Books has a rich description, authors, and exact date but sometimes no
cover. Showing them as two near-duplicate cards is noise; merging them
into one candidate gives "as many infos as possible" in a single pick.

Only the *book* sources are merged. Wookieepedia ISBN results (rich,
authoritative Star Wars comic records) pass through untouched and stay
their own card.
"""

from __future__ import annotations

from typing import Optional

from app.services.schemas import CreatorRef, LookupCandidate

# Sources that describe the same physical book by ISBN and are safe to
# merge. Order = field-precedence preference (Google Books first: it has
# the richer records and is the chosen primary for provenance/refresh).
_BOOK_SOURCES = ("googlebooks", "openlibrary")


def _isbn_key(c: LookupCandidate) -> Optional[str]:
    return c.isbn_13 or c.isbn_10


def _is_real_cover(url: Optional[str]) -> bool:
    """A usable cover — not an Open Library no-cover placeholder."""
    if not url:
        return False
    # OL's `?default=false` sentinel 404s rather than serving an image,
    # so it's not a real cover for merge-preference purposes.
    return "default=false" not in url


def _date_precision(d: Optional[str]) -> int:
    """Higher = more precise. '' / None < 'YYYY' < 'YYYY-MM' < 'YYYY-MM-DD'."""
    if not d:
        return 0
    return d.count("-") + 1


def _merge_pair(primary: LookupCandidate, others: list[LookupCandidate]) -> LookupCandidate:
    """Fold `others` into `primary` (which is already the
    highest-precedence source). Field rules favour the most complete
    value across all sources in the group."""
    group = [primary, *others]

    def first(attr):
        for c in group:
            v = getattr(c, attr)
            if v:
                return v
        return None

    def longest(attr):
        best = None
        for c in group:
            v = getattr(c, attr)
            if v and (best is None or len(v) > len(best)):
                best = v
        return best

    # Title: the most complete (a subtitle makes it longer).
    title = longest("title") or primary.title
    # Description: Google Books usually wins, but take the longest overall.
    description = longest("description")
    # Cover: prefer a real (non-placeholder) cover from any source.
    cover_url = next((c.cover_url for c in group if _is_real_cover(c.cover_url)), None)
    # Date: the most precise.
    cover_date = max((c.cover_date for c in group), key=_date_precision, default=None)

    # Authors: union across sources, de-duped case-insensitively.
    seen: set[str] = set()
    creators: list[CreatorRef] = []
    for c in group:
        for cr in c.creators:
            k = (cr.name or "").casefold()
            if k and k not in seen:
                seen.add(k)
                creators.append(cr)

    merged = primary.model_copy(update={
        "title": title,
        "series": first("series"),
        "publisher": first("publisher"),
        "description": description,
        "cover_url": cover_url,
        "cover_date": cover_date,
        "page_count": first("page_count"),
        "language": first("language"),
        "isbn_13": first("isbn_13"),
        "isbn_10": first("isbn_10"),
        "creators": creators,
    })
    # Provenance label for the picker card.
    merged.raw = {**(primary.raw or {}), "merged_from": [c.source for c in group]}
    return merged


def merge_book_candidates(candidates: list[LookupCandidate]) -> list[LookupCandidate]:
    """Return a candidate list where book-source candidates sharing an
    ISBN are merged into one. Non-book candidates and singletons pass
    through unchanged. Output order preserves first appearance."""
    if not candidates:
        return candidates

    # Bucket book candidates by ISBN; remember first-seen position so the
    # merged card lands where its first contributor did.
    groups: dict[str, list[LookupCandidate]] = {}
    order: list[object] = []          # ISBN keys (for groups) or candidates (passthrough)
    seen_keys: set[str] = set()

    for c in candidates:
        key = _isbn_key(c) if c.source in _BOOK_SOURCES else None
        if key:
            if key not in seen_keys:
                seen_keys.add(key)
                order.append(("group", key))
            groups.setdefault(key, []).append(c)
        else:
            order.append(("pass", c))

    out: list[LookupCandidate] = []
    for kind, ref in order:
        if kind == "pass":
            out.append(ref)
            continue
        members = groups[ref]
        if len(members) == 1:
            out.append(members[0])
            continue
        # Order members by source preference, then merge.
        members.sort(key=lambda c: _BOOK_SOURCES.index(c.source)
                     if c.source in _BOOK_SOURCES else len(_BOOK_SOURCES))
        out.append(_merge_pair(members[0], members[1:]))
    return out
