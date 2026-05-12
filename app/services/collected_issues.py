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
