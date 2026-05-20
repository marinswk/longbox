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
from typing import Optional

# Match a leading "COLLECTING:" / "Collects:" / "Collecting" prose header.
# Used to strip the prefix from the first entry so what follows can be
# inspected on its own merits.
_COLLECTING_PREFIX = re.compile(r"^\s*collect(?:s|ing)\b\s*:?\s*", re.IGNORECASE)

# Anything in here disqualifies an entry from being an article-title link.
_NON_TITLE_CHARS = re.compile(r"[,;#]")

# The book half of a combined StoryCite entry must end with a
# space-separated issue number ("Revelations (2023) 1", "Star Wars
# Tales 16"). A trailing letter suffix ("12A") is allowed.
_BOOK_ENDS_WITH_ISSUE_NUM = re.compile(r"\S\s+\d+[A-Za-z]?$")


def _split_combined_paren(text: str) -> Optional[tuple[str, str]]:
    """Detect a `"<story> (<book>)"` combined StoryCite entry and
    return `(story, book)`, or `None` when `text` isn't one.

    The book half is the LAST balanced parenthetical group, found by
    walking back from the final `)` and counting paren depth. This is
    what lets a book half that ITSELF contains parens be extracted —
    e.g. `"Tool of the Empire (Revelations (2023) 1)"` yields
    `("Tool of the Empire", "Revelations (2023) 1")`, where a plain
    `[^()]+` regex would choke on the nested `(2023)` year tag.

    The book must end with a space-separated issue number so plain
    parenthetical titles like `"Star Wars (1977)"` (no trailing
    number) aren't mistaken for combined entries.
    """
    if not text.endswith(")"):
        return None
    depth = 0
    open_idx = -1
    for i in range(len(text) - 1, -1, -1):
        ch = text[i]
        if ch == ")":
            depth += 1
        elif ch == "(":
            depth -= 1
            if depth == 0:
                open_idx = i
                break
    if open_idx <= 0:
        return None
    story = text[:open_idx].strip()
    book = text[open_idx + 1:-1].strip()
    if not story or not book:
        return None
    if not _BOOK_ENDS_WITH_ISSUE_NUM.search(book):
        return None
    return story, book


@dataclass
class CollectedEntry:
    text: str
    linkable: bool
    # Optional override: the Wookieepedia article title to LINK to /
    # resolve via canonical-series inference, when the display text
    # carries additional descriptive context. Example:
    #   text       = "Untitled Pizzazz Star Wars Story — Pizzazz 1"
    #   article_id = "Pizzazz 1"  ← the actual wiki article
    # When None, callers use `text` itself as the article title.
    article_id: Optional[str] = None


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
        # Prefer the article_id when set (em-dash combined entries
        # use it to point at the book reference, not the descriptive
        # "Story — Book" display string).
        article = entry.article_id or entry.text
        m = _SERIES_FROM_ISSUE.match(article)
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
            name_guess=name, sample_issue_title=article,
        ))
    return out


def derive_series_names(raw: str | None) -> list[str]:
    """Backward-compat helper returning just the trailing-number-
    stripped names. Prefer `derive_inferred_series` for new code so
    you get the sample issue title needed for canonical resolution."""
    return [g.name_guess for g in derive_inferred_series(raw)]


def parse_entries(raw: str | None) -> list[CollectedEntry]:
    """Split `raw` on newlines and classify each non-empty entry.

    Special-cases:
      * "COLLECTING:" prose prefix is stripped from the first entry,
        and if what remains is a multi-item list ("A 1-5, B 1") the
        whole entry is kept verbatim and marked `linkable=False`.
      * `"Story (Book)"` paren-combined entries emitted by the
        StoryCite parser: the display text keeps both halves, but
        `article_id` is set to just the book half so callers
        resolving / linking go straight to the real wiki article.
        Detection requires a trailing parens pair AND the inner
        text matching an article-title shape (with a trailing
        number — magazines like "Pizzazz 1" or "Star Wars Weekly 60"
        always have one). The number requirement is what stops
        unrelated parenthetical titles like "Star Wars (1977)"
        being mis-treated as combined entries.
    """
    if not raw:
        return []
    out: list[CollectedEntry] = []
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        # Trailing-parens combined entry: "Story (Book N)". We need
        # the LEFT-of-paren part to ALSO look like a clean entry —
        # otherwise free-form prose like
        #   "COLLECTING: Star Wars: Revelations (2023) 1 (Story 6)"
        # would be mistaken for a combined entry just because it
        # happens to end with parens-wrapped digits.
        combined = _split_combined_paren(text)
        if combined:
            story_part, inner = combined
            if (
                _looks_like_article_title(inner)
                and _looks_like_article_title(story_part)
            ):
                out.append(CollectedEntry(
                    text=text, linkable=True, article_id=inner,
                ))
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


def coverage_titles(raw: str | None) -> set[str]:
    """Every issue / story title a `collected_issues` blob covers, for
    series-progress and duplicate matching.

    A plain entry contributes just its own title. A combined StoryCite
    entry — `"<story> (<book N>)"` — contributes THREE keys:

      * the story title   ("Tool of the Empire")
      * the book title    ("Revelations (2023) 1")
      * the verbatim line ("Tool of the Empire (Revelations (2023) 1)")

    The story key is what attributes an anthology one-shot's contents
    to each contributing series. Wookieepedia lists the story
    ("Tool of the Empire") as an issue of *Darth Vader (2020)* even
    though it was physically published inside the multi-story one-shot
    *Revelations (2023) 1* — so a trade that collects the story has to
    match the series on the story name, not on the host book.
    """
    out: set[str] = set()
    for e in parse_entries(raw):
        if not e.linkable:
            continue
        out.add(e.text)
        if e.article_id:
            # Combined entry: article_id is the book half.
            out.add(e.article_id)
            combined = _split_combined_paren(e.text)
            if combined:
                out.add(combined[0])  # story half
    return out
