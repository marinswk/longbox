"""Display-side helpers for `Comic.collected_issues`.

The column accepts free-form values from any source — Wookieepedia
`==Contents==` lists, ComicVine description prose, the user's CSV
import, or the manual edit form. The detail page used to wrap every
line in a Wookieepedia article URL, which produced broken links for
Marvel-style "COLLECTING: A 1-5, B 1" prose.

`parse_entries()` returns a list of `{text, linkable}` dicts:

  * `linkable=True`  — the entry is a clean Wookieepedia article title
                       (e.g. "Knights of the Old Republic 1") and can
                       safely be rendered as an anchor.
  * `linkable=False` — the entry is prose / a list / a "COLLECTING:"
                       header / contains range markers; render as
                       plain text.

The rule of thumb: anything containing a comma, semicolon, hash, or a
"Collect..." prefix isn't a single article title.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Match a leading "COLLECTING:" / "Collects:" / "Collecting" prose header.
# Used to strip the prefix from the first entry so what follows can be
# inspected on its own merits.
_COLLECTING_PREFIX = re.compile(r"^\s*collect(?:s|ing)\b\s*:?\s*", re.IGNORECASE)

# Anything in here disqualifies an entry from being an article-title link.
_NON_TITLE_CHARS = re.compile(r"[,;#]")


@dataclass
class CollectedEntry:
    text: str
    linkable: bool


def _looks_like_article_title(s: str) -> bool:
    """Heuristic: a clean Wookieepedia article title contains no list
    delimiters (',' ';'), no issue-range markers ('#'), and isn't
    excessively long. Parens are fine — many titles include "(YYYY)".
    """
    if not s:
        return False
    if _NON_TITLE_CHARS.search(s):
        return False
    if len(s) > 120:
        return False
    return True


# Match a Wookieepedia-style "<Series Name> <Issue Number>" article
# title, capturing the series-name portion. Issue numbers can be plain
# integers, with an optional letter suffix ("12A"), or "0" (zero
# issues are a real thing on Wookieepedia). The series name greedily
# matches everything up to the LAST space-separated digit run.
_SERIES_FROM_ISSUE = re.compile(
    r"^(?P<series>.+?)\s+(?P<num>\d+[A-Za-z]?)$"
)


@dataclass
class InferredSeriesGroup:
    """One distinct series implied by a TPB/omnibus's collected_issues.

    `name_guess` is the trailing-number-stripped portion — useful as a
    fallback when we can't reach upstream to get the canonical name.

    `sample_issue_title` is a real issue article title from the
    collected_issues list. Callers pass it to
    `wookieepedia.get_article(...)` to read the canonical series
    article title off the issue's infobox — way more reliable than
    name-matching, since the issue infobox's `series=` field is the
    authoritative wikilink to the series page.
    """
    name_guess: str
    sample_issue_title: str


def derive_inferred_series(raw: str | None) -> list[InferredSeriesGroup]:
    """Walk a `collected_issues` blob and return one entry per
    unique implied series.

    De-duped case-insensitively on the trailing-number-stripped name,
    in first-seen order, so callers can drive a small fan-out of
    Wookieepedia lookups (one per distinct series, not one per
    issue) to discover canonical series article titles.
    """
    if not raw:
        return []
    seen_norm: set[str] = set()
    out: list[InferredSeriesGroup] = []
    for entry in parse_entries(raw):
        if not entry.linkable:
            continue
        m = _SERIES_FROM_ISSUE.match(entry.text)
        if not m:
            continue
        name = m.group("series").strip()
        if not name:
            continue
        norm = name.casefold()
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        out.append(InferredSeriesGroup(
            name_guess=name, sample_issue_title=entry.text,
        ))
    return out


def derive_series_names(raw: str | None) -> list[str]:
    """Backward-compat helper returning just the trailing-number-
    stripped names. Prefer `derive_inferred_series` for new code so
    you get the sample issue title needed for canonical resolution."""
    return [g.name_guess for g in derive_inferred_series(raw)]


def parse_entries(raw: str | None) -> list[CollectedEntry]:
    """Split `raw` on newlines and classify each non-empty entry.

    Special-cases the "COLLECTING:" prose pattern: the prefix is stripped
    from the first entry, and if what remains still has a comma (i.e.
    it's a "A 1-5, B 1" list, not a single title) the whole entry is
    kept verbatim and marked `linkable=False`.
    """
    if not raw:
        return []
    out: list[CollectedEntry] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        # Strip the "COLLECTING:" prefix on the very first entry so it
        # doesn't mask everything after it as non-linkable.
        head = _COLLECTING_PREFIX.sub("", text)
        had_prefix = head != text
        # If the prefix-stripped value is still a multi-item list (or any
        # other non-title shape) keep the original text + don't link.
        if had_prefix or not _looks_like_article_title(head):
            out.append(CollectedEntry(text=text, linkable=False))
        else:
            out.append(CollectedEntry(text=head, linkable=True))
    return out
