"""Shared series-name derivation from trade titles.

Book sources (Open Library, Google Books) rarely supply a real series
field, and Metron's `series_name` is a literal filter — both need the
"series base" extracted from a volume title:

    "Saga, Volume Five"            -> "Saga"
    "The Sandman Vol. 1: Preludes" -> "The Sandman"

Centralised here so the three callers don't each carry their own copy
of the regex.
"""

from __future__ import annotations

import re
from typing import Optional

# A trailing volume marker: separator + marker + everything after.
#
# `Vol/Volume/Book/Part` must be followed by a NUMBER (digit or
# number-word) to count — otherwise "Blue Book" / "Free Comic Book"
# would be wrongly truncated to "Blue" / "Free Comic". `TP`/`TPB` are
# standalone format markers (no number).
#
# `Compendium` / `Omnibus` / `Deluxe` are deliberately NOT markers —
# those are distinct collected editions, not entries in the numbered run.
_NUM = (
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|"
    r"eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|"
    r"eighteen|nineteen|twenty)"
)
_VOLUME_MARKER = re.compile(
    r"[\s:,\-–—]+(?:(?:vol\.?|volume|book|part)\s*" + _NUM + r"|tpb?)\b.*$",
    re.IGNORECASE,
)


def strip_volume_marker(text: Optional[str]) -> str:
    """Reduce a title/query to its series-name base by stripping a
    trailing volume marker. Returns the original (trimmed) string when
    there's nothing to strip — used by Metron's literal series filter,
    which still wants *something* to query."""
    q = (text or "").strip()
    base = _VOLUME_MARKER.sub("", q).strip(" :,-–—")
    return base or q


def derive_series(title: Optional[str]) -> Optional[str]:
    """Best-effort series name from a trade title, or ``None`` when no
    volume marker is present (so the caller's title-as-series fallback
    applies). Used by Open Library / Google Books to group volumes:

        "Saga, Volume Five"  -> "Saga"
        "Dream Country"      -> None   (no marker — can't derive)
    """
    if not title:
        return None
    base = strip_volume_marker(title)
    if base and base != title.strip():
        return base
    return None
