"""Wookieepedia (Star Wars Fandom wiki) metadata client.

Wookieepedia runs on MediaWiki, so we use the public Action API at
https://starwars.fandom.com/api.php — no scraping, no key required.

Coverage is unmatched for Star Wars across publishers (Marvel, Dark Horse,
IDW, Disney) and decades (1977 → present). For non-SW comics this source
returns nothing, which is fine — the aggregator just picks up Open Library.

Three lookups are exposed:
  - search_isbn(isbn)  → {{ComicCollection}} infobox for trades
  - search_upc(upc)    → {{ComicBook}} infobox for single issues; UPCs may
                          match multiple issues when the wiki only stores the
                          series-level 12-digit code, in which case the picker
                          disambiguates.
  - get_article(title) → direct fetch by article title (used internally and
                          available for future title-search flows).

Each lookup makes at most three API calls per ISBN/UPC, all cached via
MetadataCache (source="wookieepedia") so re-scans don't hammer Fandom.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import httpx

from app.config import settings
from app.services.cache import get_or_set
from app.services.errors import UpstreamRateLimit
from app.services.schemas import CreatorRef, LookupCandidate

API_URL = "https://starwars.fandom.com/api.php"
SOURCE = "wookieepedia"
SEARCH_LIMIT = 5
TEXT_SEARCH_LIMIT = 20
INFOBOX_TEMPLATES = {"comiccollection", "comicbook"}


def is_configured() -> bool:
    return True


# ---------------------------------------------------------------------------
# Wikitext template parsing
# ---------------------------------------------------------------------------


def _split_top_level_params(body: str) -> list[str]:
    """Split a template body by top-level '|' separators.

    Skips '|' chars that live inside nested {{...}} or [[...]] constructs.
    """
    parts: list[str] = []
    buf: list[str] = []
    brace = 0
    bracket = 0
    i = 0
    while i < len(body):
        if body[i:i + 2] == "{{":
            brace += 1; buf.append(body[i:i + 2]); i += 2
        elif body[i:i + 2] == "}}":
            brace -= 1; buf.append(body[i:i + 2]); i += 2
        elif body[i:i + 2] == "[[":
            bracket += 1; buf.append(body[i:i + 2]); i += 2
        elif body[i:i + 2] == "]]":
            bracket -= 1; buf.append(body[i:i + 2]); i += 2
        elif body[i] == "|" and brace == 0 and bracket == 0:
            parts.append("".join(buf)); buf = []; i += 1
        else:
            buf.append(body[i]); i += 1
    parts.append("".join(buf))
    return parts


_LINK_PIPE = re.compile(r"\[\[([^\]\|]+)\|([^\]]+)\]\]")
_LINK_PLAIN = re.compile(r"\[\[([^\]]+)\]\]")
_REF_BLOCK = re.compile(r"<ref[^>]*>.*?</ref>", re.DOTALL)
_REF_SELFCLOSE = re.compile(r"<ref[^/]*/>")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BOLD_ITALIC = re.compile(r"'{2,5}")
_INNER_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_BR_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)
_LEADING_BULLET = re.compile(r"^\s*\*+\s*", re.MULTILINE)


def _clean(value: str) -> str:
    """Strip wiki markup down to a plain-text rendering."""
    s = value
    s = _HTML_COMMENT.sub("", s)
    s = _REF_BLOCK.sub("", s)
    s = _REF_SELFCLOSE.sub("", s)
    s = _BR_TAG.sub(" ", s)
    s = _LINK_PIPE.sub(r"\2", s)
    s = _LINK_PLAIN.sub(r"\1", s)
    s = _BOLD_ITALIC.sub("", s)
    # remove embedded templates (best-effort, non-recursive — good enough)
    while _INNER_TEMPLATE.search(s):
        s = _INNER_TEMPLATE.sub("", s)
    # Wikitext list bullets at the start of a value: "*Foo\n*Bar" → "Foo\nBar".
    s = _LEADING_BULLET.sub("", s)
    # collapse runs of whitespace within a single line, but keep newlines so
    # multi-value fields (e.g. "*A\n*B") render readably.
    s = re.sub(r"[ \t]+", " ", s)
    return s.strip()


def _first_line(value: Optional[str]) -> Optional[str]:
    """For single-value infobox fields that can accidentally carry a
    multi-value blob (e.g. ComicBook `series=` listing both an original
    title and a relaunch), return the first non-empty stripped line.
    `None` / empty → `None`."""
    if not value:
        return None
    for line in value.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _find_infobox(wikitext: str) -> Optional[dict[str, str]]:
    """Walk top-level templates and return the first ComicBook/ComicCollection
    one as a {param_name: cleaned_value} dict. None if not found.
    """
    i = 0
    while i < len(wikitext):
        if wikitext[i:i + 2] == "{{":
            depth = 1
            j = i + 2
            while j < len(wikitext) - 1 and depth:
                if wikitext[j:j + 2] == "{{":
                    depth += 1; j += 2
                elif wikitext[j:j + 2] == "}}":
                    depth -= 1; j += 2
                else:
                    j += 1
            body = wikitext[i + 2:j - 2]
            params = _split_top_level_params(body)
            head = params[0].strip().split("\n", 1)[0]
            if head.lower().strip() in INFOBOX_TEMPLATES:
                fields: dict[str, str] = {}
                for raw in params[1:]:
                    if "=" not in raw:
                        continue
                    key, _, val = raw.partition("=")
                    fields[key.strip().lower()] = _clean(val)
                fields["__template__"] = head.strip()
                return fields
            i = j
        else:
            i += 1
    return None


_DATE_FULL = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+(\d{4})\b",
    re.IGNORECASE,
)
_YEAR_ONLY = re.compile(r"\b(19|20)\d{2}\b")
_MONTHS = {m.lower(): i for i, m in enumerate(
    "January February March April May June July August September October November December".split(), 1
)}


def _parse_date(text: str) -> Optional[str]:
    if not text:
        return None
    m = _DATE_FULL.search(text)
    if m:
        return f"{m.group(3)}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
    m = _YEAR_ONLY.search(text)
    if m:
        return m.group(0)
    return None


def _strip_filename(image_field: str) -> Optional[str]:
    """Pull a filename out of `[[File:Foo.jpg]]` or `[[File:Foo.jpg|right|thumb]]`."""
    m = re.search(r"File:([^\]\|]+)", image_field)
    if m:
        return m.group(1).strip()
    return image_field.strip() or None


# ---------------------------------------------------------------------------
# HTTP layer (cached)
# ---------------------------------------------------------------------------


def _headers() -> dict[str, str]:
    return {"User-Agent": settings.comicvine_user_agent, "Accept": "application/json"}


async def _api(client: httpx.AsyncClient, params: dict[str, Any]) -> dict[str, Any]:
    r = await client.get(API_URL, params={**params, "format": "json"}, headers=_headers())
    if r.status_code == 429:
        raise UpstreamRateLimit(SOURCE, "Fandom is throttling requests")
    r.raise_for_status()
    return r.json()


async def _search_titles(query: str, limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    async def fetch() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await _api(client, {
                "action": "query", "list": "search",
                "srsearch": query, "srlimit": limit,
            })
    # Cache key includes the limit so a wider text search doesn't return
    # the cached 5-row identifier search.
    payload = await get_or_set(source=SOURCE, key=f"search:{limit}:{query}", fetch=fetch)
    return payload.get("query", {}).get("search", []) or []


async def _parse_page(title: str) -> Optional[dict[str, Any]]:
    async def fetch() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await _api(client, {
                "action": "parse", "page": title, "prop": "wikitext",
                "redirects": 1,
            })
    payload = await get_or_set(source=SOURCE, key=f"page:{title}", fetch=fetch)
    return payload.get("parse")


async def _resolve_image_url(filename: str) -> Optional[str]:
    async def fetch() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            return await _api(client, {
                "action": "query", "titles": f"File:{filename}",
                "prop": "imageinfo", "iiprop": "url",
            })
    payload = await get_or_set(source=SOURCE, key=f"image:{filename}", fetch=fetch)
    pages = (payload.get("query") or {}).get("pages") or {}
    for _, page in pages.items():
        infos = page.get("imageinfo") or []
        if infos:
            return infos[0].get("url")
    return None


# ---------------------------------------------------------------------------
# Candidate construction
# ---------------------------------------------------------------------------


_TOP_TEMPLATE = re.compile(r"\{\{[Tt]op\b([^}]*)\}\}")


def _detect_canon(wikitext: str) -> Optional[str]:
    """The {{Top}} template at the top of every Wookieepedia article carries
    canon-vs-Legends flags as positional args: `can` (canon), `leg` (Legends),
    `ncc` (non-canon). If neither flag is present we leave it null rather
    than guessing.
    """
    m = _TOP_TEMPLATE.search(wikitext)
    if not m:
        return None
    args = {a.strip().lower() for a in m.group(1).split("|") if a.strip()}
    if "leg" in args or "legends" in args:
        return "legends"
    if "can" in args or "canon" in args:
        return "canon"
    return None


_HEADING = re.compile(r"^==[^=].*?==\s*$", re.MULTILINE)
_FILE_OR_TEMPLATE_LINE = re.compile(r"^\s*(\[\[File:|\{\{)", re.MULTILINE)


def _extract_lead_paragraph(wikitext: str) -> Optional[str]:
    """Grab the first body paragraph after the infobox closes, before the
    first section heading. Strips wiki markup down to plain text."""
    # Cut off at the first heading (== Plot ==, == Appearances ==, etc.).
    end = _HEADING.search(wikitext)
    body = wikitext[: end.start()] if end else wikitext

    # Drop everything before the close of the last top-level template at the
    # head of the article (the {{Top}} + the infobox).
    depth = 0
    last_close = 0
    i = 0
    while i < len(body):
        if body[i:i + 2] == "{{":
            depth += 1
            i += 2
        elif body[i:i + 2] == "}}":
            depth -= 1
            i += 2
            if depth == 0:
                last_close = i
        else:
            i += 1
    body = body[last_close:]

    # Take the first non-empty paragraph that isn't itself a stray template
    # or file include.
    for chunk in re.split(r"\n\s*\n", body):
        chunk = chunk.strip()
        if not chunk:
            continue
        if _FILE_OR_TEMPLATE_LINE.match(chunk):
            continue
        cleaned = _clean(chunk)
        if cleaned:
            return cleaned
    return None


_CONTENTS_HEADER = re.compile(
    r"^==\s*(Contents|Collects|Collected\s+issues|Issues\s+collected)\s*==\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Series-article section heading variants. Wookieepedia uses either a
# level-2 (==Issues==) or level-3 (===Issues===, nested inside ==Media==)
# heading depending on the article's age — accept both.
_SERIES_ISSUES_HEADERS = re.compile(
    r"^={2,3}\s*(?:List\s+of\s+)?(?:Single[- ])?Issues\s*={2,3}\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Trade-paperback / collection series like "Epic Collection",
# "Marvel Omnibus", "Marvel Modern Era" list their member volumes
# under headings like ==Volumes==, ==Editions==, ==Trade paperbacks==,
# ==Books==, ==Releases==, ==Collections==, ==Installments==. None of
# those match the "Issues" regex above, which is why TPB series
# rendered with an empty expected-issues list until now.
_SERIES_VOLUMES_HEADERS = re.compile(
    r"^={2,3}\s*"
    r"(?:List\s+of\s+)?"
    r"(?:Volumes?"
    r"|Editions?"
    r"|Trade\s+paperbacks?"
    r"|Trades"
    r"|Books"
    r"|Releases"
    r"|Collections?"
    r"|Omnibuses?"
    r"|Hardcovers?"
    r"|Installments?"
    r")\s*={2,3}\s*$",
    re.MULTILINE | re.IGNORECASE,
)

# Prettytable / wikitable rows in TPB series articles (most notably
# Star Wars Omnibus) format each volume as:
#   |rowspan="4"|'''''1. [[Article Title]]'''''
# Match the number + period + opening wikilink so we pick up the
# article title without dragging in collected story-arc wikilinks
# from sibling cells. Anchored to `^|\|` to dodge stray matches in
# prose ("section 1. The introduction..." is too rare to worry about
# but the anchor costs us nothing).
_NUMBERED_VOLUME_RX = re.compile(
    r"(?:^|\|)\s*'*\s*\d+\.\s*'*\s*\[\[([^\]\|]+)",
    re.MULTILINE,
)


def _extract_numbered_volume_links(wikitext: str) -> list[str]:
    """Find wikitable rows of the shape `N. [[Article]]` and return
    each Article in first-seen order. De-duplicates so an article
    appearing in multiple table rows (rare but possible) lists once."""
    out: list[str] = []
    seen: set[str] = set()
    for m in _NUMBERED_VOLUME_RX.finditer(wikitext):
        title = m.group(1).strip()
        low = title.lower()
        if low.startswith(("file:", "image:", "category:")):
            continue
        if title in seen:
            continue
        seen.add(title)
        out.append(title)
    return out
_NEXT_HEADING_OR_TABLE_END = re.compile(
    r"^(?:={2,3}[^=]|\{\{Comictable-end)", re.MULTILINE
)
_LINK_ARTICLE = re.compile(r"\[\[([^\]\|]+)")
_STORYCITE = re.compile(r"\{\{StoryCite\b([^}]*)\}\}")


def _extract_storycite_label(line: str) -> Optional[str]:
    """Pull a usable label out of a `{{StoryCite|story=…|book=…}}` template,
    preferring the story title (the named `story=` arg). Falls back to the
    `book=` arg, then the first positional arg."""
    m = _STORYCITE.search(line)
    if not m:
        return None
    args = [a.strip() for a in m.group(1).split("|") if a.strip()]
    named: dict[str, str] = {}
    positional: list[str] = []
    for a in args:
        if "=" in a:
            k, _, v = a.partition("=")
            named[k.strip().lower()] = v.strip()
        else:
            positional.append(a)
    return named.get("story") or named.get("book") or (positional[0] if positional else None)


def _extract_bullet_targets(body: str) -> list[str]:
    """Walk a wikitext block of `*`-bullets and return a usable target
    string per bullet — preferring (in order):
        1. the article title from a `[[Foo|Bar]]` / `[[Foo]]` wikilink;
        2. the `story=` (or `book=`) field of a `{{StoryCite|…}}` template;
        3. the cleaned plain-text rendering of the line.
    """
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("*"):
            continue
        line = re.sub(r"^\*+\s*", "", line)
        link = _LINK_ARTICLE.search(line)
        if link:
            out.append(link.group(1).strip())
            continue
        story = _extract_storycite_label(line)
        if story:
            out.append(story)
            continue
        cleaned = _clean(line)
        if cleaned:
            out.append(cleaned)
    return out


def _section_body(wikitext: str, header_re: re.Pattern[str]) -> Optional[str]:
    """Return the wikitext between the first match of `header_re` and the
    next heading (any depth) or end-of-table marker. None if no match."""
    m = header_re.search(wikitext)
    if not m:
        return None
    body = wikitext[m.end():]
    nxt = _NEXT_HEADING_OR_TABLE_END.search(body)
    if nxt:
        body = body[: nxt.start()]
    return body


def _extract_section_bullets(wikitext: str, header_re: re.Pattern[str]) -> list[str]:
    """Bullet-list parser for a section starting at the first match of
    `header_re` and ending at the next heading. Uses the same shared
    bullet-target logic as the series and contents parsers."""
    body = _section_body(wikitext, header_re)
    if body is None:
        return []
    return _extract_bullet_targets(body)


def _extract_comictable_issues(body: str) -> list[str]:
    """Walk every top-level `{{Comictable-issue|N|[[Article|...]]|...}}`
    invocation in `body` and return the article title from each row.

    Modern Wookieepedia series articles list their issues in this template
    rather than a bullet list. The template's second positional arg is the
    title cell; the first wikilink inside it points at the issue's article.
    """
    titles: list[str] = []
    i = 0
    needle = "{{Comictable-issue|"
    while True:
        start = body.find(needle, i)
        if start == -1:
            break
        depth = 1
        j = start + len(needle)
        while j < len(body) - 1 and depth:
            if body[j:j + 2] == "{{":
                depth += 1; j += 2
            elif body[j:j + 2] == "}}":
                depth -= 1; j += 2
            else:
                j += 1
        inner = body[start + 2:j - 2]  # strip leading '{{' and trailing '}}'
        # _split_top_level_params returns ['Comictable-issue', N, title_cell, …].
        params = _split_top_level_params(inner)
        if len(params) >= 3:
            title_cell = params[2]
            link = _LINK_ARTICLE.search(title_cell)
            if link:
                titles.append(link.group(1).strip())
        i = j
    return titles


def _extract_contents_section(wikitext: str) -> list[str]:
    """Parse the `==Contents==` (or equivalent) bullet list on a trade
    article. Each bullet typically wikilinks to a single-issue article;
    `{{StoryCite|story=…}}` templates are recognised so short stories
    don't get silently dropped.
    """
    return _extract_section_bullets(wikitext, _CONTENTS_HEADER)


# TPB-series articles like Epic Collection, Star Wars Omnibus, and
# Modern Era list their member volumes inside `<gallery>` blocks
# (typically under ==Media==/===Legends===/===Canon=== subheadings)
# rather than bullet lists. Each gallery line is:
#   File:CoverArt.png|''[[Article Title|Display Text]]''<br />...
# The first wikilink on the line is the article we want; subsequent
# wikilinks are dates / authors / footnote refs we should ignore.
_GALLERY_BLOCK = re.compile(
    r"<gallery\b[^>]*>(.*?)</gallery>", re.DOTALL | re.IGNORECASE
)


def _extract_gallery_links(wikitext: str) -> list[str]:
    """Pull the first wikilinked article title from each line inside
    every `<gallery>` block in `wikitext`. De-duplicates while
    preserving first-seen order so a volume that appears in both
    ===Legends=== and ===Canon=== galleries doesn't show up twice.
    Lines whose first wikilink points at a `File:` / `Image:` asset
    are skipped — those are the gallery's image declarations, not
    article links."""
    out: list[str] = []
    seen: set[str] = set()
    for block in _GALLERY_BLOCK.finditer(wikitext):
        body = block.group(1)
        for raw in body.splitlines():
            line = raw.strip()
            if not line:
                continue
            # First wikilink on the line — that's the article title in
            # the gallery convention `File:X.png|''[[Article|...]]''`.
            m = _LINK_ARTICLE.search(line)
            if not m:
                continue
            title = m.group(1).strip()
            # Defensive: a malformed line might wikilink the image
            # itself. Skip namespace prefixes that aren't articles.
            low = title.lower()
            if low.startswith(("file:", "image:", "category:")):
                continue
            if title in seen:
                continue
            seen.add(title)
            out.append(title)
    return out


def _split_multivalue(raw: str) -> list[str]:
    """Wookieepedia uses bullets, newlines, and the rare comma to list
    multiple values inside one infobox field. _clean already stripped the
    bullet markers, so split on newlines first; fall back to commas only if
    we ended up with a single line."""
    lines = [s.strip() for s in raw.split("\n") if s.strip()]
    if len(lines) > 1:
        return lines
    if lines and "," in lines[0]:
        return [s.strip() for s in lines[0].split(",") if s.strip()]
    return lines


_APP_HEADING_RE = re.compile(r"==\s*Appearances\s*==", re.IGNORECASE)
_NEXT_LV2_RE = re.compile(r"\n==[^=].*?==", re.DOTALL)
_CHARS_HEADING_RE = re.compile(
    r"={3,}\s*Characters?\s*={3,}\s*(.*?)(?:={3,}\s*\w|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_WIKILINK_RE = re.compile(r"\[\[([^\]\|\n]+?)(?:\|([^\]\n]+?))?\]\]")
_NON_CHARACTER_PREFIXES = ("File:", "Category:", "Image:")


def _extract_appearances_characters(wikitext: str, *, limit: int = 30) -> list[str]:
    """Return character names from the `==Appearances==` section of an
    article. Prefers a `===Characters===` subsection when present; otherwise
    falls back to the whole Appearances block.

    Captures `[[Foo]]` and `[[Foo|Bar]]` (where Bar is the display text).
    Strips obvious non-character links (Files, Categories) and parenthetical
    disambiguators handled later by the auto-tagger.
    """
    if not wikitext:
        return []
    m = _APP_HEADING_RE.search(wikitext)
    if not m:
        return []
    after = wikitext[m.end():]
    # Bound at the next level-2 heading.
    end_match = _NEXT_LV2_RE.search(after)
    section = after[: end_match.start()] if end_match else after

    chars_match = _CHARS_HEADING_RE.search(section)
    target = chars_match.group(1) if chars_match else section

    seen: set[str] = set()
    out: list[str] = []
    for link, display in _WIKILINK_RE.findall(target):
        name = (display or link).strip()
        if not name or any(name.startswith(p) for p in _NON_CHARACTER_PREFIXES):
            continue
        # Drop numeric-only links (asterisk lists sometimes contain them).
        if name.isdigit():
            continue
        # De-dup case-insensitively but keep the first-seen casing.
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
        if len(out) >= limit:
            break
    return out


async def _candidate_from_title(title: str) -> Optional[LookupCandidate]:
    parsed = await _parse_page(title)
    if not parsed:
        return None
    wikitext = (parsed.get("wikitext") or {}).get("*") or ""
    fields = _find_infobox(wikitext)
    if not fields:
        return None

    cover_url: Optional[str] = None
    image_field = fields.get("image")
    if image_field:
        filename = _strip_filename(image_field)
        if filename:
            try:
                cover_url = await _resolve_image_url(filename)
            except Exception:
                cover_url = None

    creators: list[CreatorRef] = []
    for role in ("writer", "penciller", "inker", "letterer", "colorist", "cover artist", "editor"):
        raw_value = fields.get(role, "")
        if not raw_value:
            continue
        for name in _split_multivalue(raw_value):
            creators.append(CreatorRef(name=name, role=role))

    isbn = fields.get("isbn") or ""
    isbn_digits = "".join(ch for ch in isbn if ch.isdigit())

    upc_raw = fields.get("upc") or ""
    upc_digits = "".join(ch for ch in upc_raw if ch.isdigit()) or None

    page_count_raw = fields.get("pages") or fields.get("page count") or ""
    page_count_match = re.search(r"\d+", page_count_raw)
    page_count = int(page_count_match.group()) if page_count_match else None

    issue_number = _first_line(fields.get("issue"))

    # Single-value text fields can carry multi-value blobs when an infobox
    # uses `<br>` / bullets to record re-launches or alt-titles. Take the
    # first non-empty line so the saved Series / Comic carry a clean,
    # single-line name regardless of the upstream wiki shape.
    title_clean = _first_line(fields.get("title")) or parsed.get("title") or title
    series = _first_line(fields.get("series"))
    publisher = _first_line(fields.get("publisher"))

    # Trade collections list their contents either inside |issues= on the
    # infobox or, more commonly, in a `==Contents==` (or `==Collects==`)
    # section as a bullet list. Try both.
    collected_issues = None
    if fields.get("__template__", "").lower() == "comiccollection":
        issues_raw = fields.get("issues") or ""
        items: list[str] = []
        if issues_raw:
            items = _split_multivalue(issues_raw)
        if not items:
            items = _extract_contents_section(wikitext)
        if items:
            collected_issues = "\n".join(items)

    # Wookieepedia's infobox uses `media type` for the binding kind ("Trade
    # paperback", "Hardcover", "Single issue", "Comic book", "Omnibus", …).
    fmt = (
        fields.get("media type")
        or fields.get("format")
        or fields.get("type")
        or None
    )
    language = fields.get("language") or None
    timeline = fields.get("timeline") or None
    era = fields.get("era") or None

    canon = _detect_canon(wikitext)
    description = _extract_lead_paragraph(wikitext)

    arcs: list[str] = []
    arc_raw = fields.get("arc") or fields.get("crossover") or ""
    if arc_raw:
        arcs = _split_multivalue(arc_raw)

    characters = _extract_appearances_characters(wikitext)

    return LookupCandidate(
        source=SOURCE,
        source_id=title,  # article title is the natural id
        title=title_clean,
        series=series,
        issue_number=issue_number,
        publisher=publisher,
        cover_date=_parse_date(fields.get("release date") or ""),
        description=description,
        cover_url=cover_url,
        isbn_13=isbn_digits if len(isbn_digits) == 13 else None,
        isbn_10=isbn_digits if len(isbn_digits) == 10 else None,
        upc=upc_digits,
        page_count=page_count,
        creators=creators,
        format=fmt,
        language=language,
        collected_issues=collected_issues,
        timeline=timeline,
        era=era,
        canon=canon,
        story_arcs=arcs,
        characters=characters,
        raw=fields,
    )


async def _search_for_candidates(query: str, limit: int = SEARCH_LIMIT) -> list[LookupCandidate]:
    hits = await _search_titles(query, limit=limit)
    candidates: list[LookupCandidate] = []
    for hit in hits:
        title = hit.get("title")
        if not title:
            continue
        cand = await _candidate_from_title(title)
        if cand is not None:
            candidates.append(cand)
    return candidates


async def search_isbn(isbn: str) -> list[LookupCandidate]:
    return await _search_for_candidates(isbn)


async def search_upc(upc: str) -> list[LookupCandidate]:
    return await _search_for_candidates(upc)


async def search_text(query: str) -> list[LookupCandidate]:
    """Free-text search across Wookieepedia article titles + content. Pulls
    up to `TEXT_SEARCH_LIMIT` hits and resolves each to a LookupCandidate
    via the same infobox parser used by ISBN/UPC lookups. Articles without
    a recognisable comic infobox are dropped silently."""
    return await _search_for_candidates(query, limit=TEXT_SEARCH_LIMIT)


async def get_article(title: str) -> Optional[LookupCandidate]:
    return await _candidate_from_title(title)


async def get_series_issues(article_title: str) -> list[str]:
    """Fetch the issue (or volume) list off a *series* article.

    Strategy, in order:
      1. ==Issues== / ===Issues=== (with "List of" / "Single" aliases).
         Inside, prefer the modern `{{Comictable-issue|…}}` template
         shape (nested under ==Media== on newer articles); fall back to
         a plain bullet list for older articles.
      2. ==Volumes== / ==Editions== / ==Trade paperbacks== etc. for
         TPB-series articles like "Epic Collection" or "Marvel
         Omnibus", which list their member volumes under those
         headings instead of an "Issues" section.
      3. ==Contents== section as a last resort (mostly for individual
         trade articles, not series).

    Returns the article titles of every entry, in upstream order.
    Empty list if the article doesn't exist or has no recognisable
    section.
    """
    parsed = await _parse_page(article_title)
    if not parsed:
        return []
    wikitext = (parsed.get("wikitext") or {}).get("*") or ""

    # Path 1: single-issue series.
    body = _section_body(wikitext, _SERIES_ISSUES_HEADERS)
    if body is not None:
        items = _extract_comictable_issues(body)
        if items:
            return items
        items = _extract_bullet_targets(body)
        if items:
            return items

    # Path 2: TPB / omnibus / epic-collection series. Volumes are
    # listed under ==Volumes== / ==Editions== / ==Installments== etc.
    # Try the three common shapes in turn: plain bullet list (typical
    # old-style articles), then a numbered prettytable (Star Wars
    # Omnibus), then a <gallery> block scoped to the section
    # (Epic Collection's ===Legends===/===Canon=== subheadings live
    # under ==Media==, so a section-scoped gallery search wins when
    # the Volumes heading is present).
    body = _section_body(wikitext, _SERIES_VOLUMES_HEADERS)
    if body is not None:
        items = _extract_bullet_targets(body)
        if items:
            return items
        items = _extract_numbered_volume_links(body)
        if items:
            return items
        items = _extract_gallery_links(body)
        if items:
            return items

    # Path 3: gallery-based articles that DON'T put their galleries
    # under a Volumes-style heading (Epic Collection puts them under
    # ==Media==/===Legends===/===Canon=== with no top-level Volumes
    # section). Scan every gallery block in the whole wikitext.
    gallery_items = _extract_gallery_links(wikitext)
    if gallery_items:
        return gallery_items

    # Path 4: numbered-table TPB-series articles without a Volumes
    # heading (Star Wars Omnibus uses ===Installments=== which IS in
    # _SERIES_VOLUMES_HEADERS now, but keep the whole-doc fallback
    # for variants we haven't catalogued).
    numbered_items = _extract_numbered_volume_links(wikitext)
    if numbered_items:
        return numbered_items

    # Path 5: trade-issue contents fallback.
    return _extract_contents_section(wikitext)
