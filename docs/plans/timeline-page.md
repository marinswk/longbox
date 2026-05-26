# Star Wars Comics Timeline page

> ⚠ **Planned, not yet implemented.** Preserved as a design doc for
> when this feature gets built. The `/timeline` route, the
> `services/timeline.py` module, and the template described below
> don't exist in the current codebase. Tracked in
> [ROADMAP.md](../../ROADMAP.md).

## Context

The app already stores in-universe chronology data on every Wookieepedia-sourced comic — `Comic.timeline` (free-text BBY/ABY string like `"3956 BBY"` or `"3964–3962 BBY"`), `Comic.era` (broader label like `"Imperial"`), `Comic.canon` (`"canon"` / `"legends"` / `None`). Nothing in the UI exposes this for chronological browsing — the user wants a `/timeline` page that organises their owned Star Wars comics by in-universe year.

User-confirmed design choices:
- **Primary view**: vertical era-grouped list, comics sorted chronologically within each era group.
- **Continuity split**: merged by default with a Canon / Legends / Both toggle.
- **Scope**: owned comics only (Copy ≥ 1) with `fandom='star wars'`. Comics with no `Comic.timeline` value bucket into an "Undated" section at the bottom.

## Recommended approach

### New parser: `app/services/timeline.py`

Tiny module — no DB, pure functions. Two helpers:

```python
def parse_timeline_value(s: str | None) -> tuple[int | None, int | None, str]:
    """('3956 BBY')           → (-3956, -3956, '3956 BBY')
       ('3964–3962 BBY')      → (-3964, -3962, '3964–3962 BBY')
       ('22 BBY')             → (-22, -22, '22 BBY')
       ('5 ABY')              → (5, 5, '5 ABY')
       ('Imperial era' / '')   → (None, None, raw)
    """
    # Regex: `(\d+)\s*(?:[-–]\s*(\d+))?\s*(BBY|ABY)`
    # BBY values are negated for sortability so older = smaller.
    # Display text echoes the raw input (already html-unescaped by _clean).

def derive_era_bucket(start_year: int | None) -> str:
    """Map a signed year onto a canonical era bucket. Buckets:
       Old Republic       year < -1000
       Rise of the Empire -1000 ≤ year < -19
       Rebellion          -19 ≤ year < 5
       New Republic       5 ≤ year < 25
       New Jedi Order     25 ≤ year < 40
       Legacy             year ≥ 40
       Undated            year is None
    """
```

Ordering for the era buckets is stable + chronological. Comics within each bucket sort by `(start_year, comic.title)`.

### New router: `app/routers/timeline.py`

`GET /timeline` — accepts query params:

| Param | Values | Default |
|---|---|---|
| `canon` | `canon`, `legends`, `both` | `both` |
| `era` | repeatable era-bucket names | none |
| `series` | repeatable series names | none |
| `q` | substring on title | empty |
| `sort` | `chrono_asc` / `chrono_desc` | `chrono_asc` |

Query shape:

```python
owned_ids = SELECT comic_id FROM copy        # owned only
comics = SELECT Comic JOIN Series JOIN Publisher
         WHERE Comic.id IN owned_ids
           AND Comic.fandom = 'star wars'
         ORDER BY <sort>
```

Then in Python: parse each `comic.timeline` via `parse_timeline_value`; bucket via `derive_era_bucket`; group, sort within group, filter.

Facets exposed to the sidebar: canon counts, era counts (post-bucket), series counts.

### New template: `app/templates/timeline.html`

Clone the layout of `app/templates/library.html`:
- Filter sidebar (hamburger drawer on mobile, sticky on desktop) with:
  - Search input.
  - Canon / Legends / Both radio chips.
  - Era checkbox-list (Old Republic / Rise of the Empire / …).
  - Series checkbox-list.
  - Sort dropdown (chrono asc / desc).
- Main column: panels per era with the era name + count, then a chronological list of comic rows.

Each row layout (text-first for density, same convention as the new duplicates page):

```
3964–3962 BBY  ·  📚  Star Wars Legends: The Old Republic Omnibus Vol. 1
                       Star Wars: Knights of the Old Republic · canon ✓
22 BBY         ·  📕  Star Wars: Darth Maul (2000) Vol. 1
                       Star Wars: Darth Maul (2000) · legends
```

Year column left-aligned in a fixed-width font-mono. Format icon (📕 single/trade, 📚 hardcover/omnibus, 📖 fallback) — same convention as duplicates. Title links to `/comic/{id}`. Series name + continuity badge below.

An "Undated" section at the bottom lists comics with no `timeline` value (kept rather than hidden so the user can fix them).

### Light chart (optional, on top of the list)

Stretch — a one-line Chart.js bar chart showing comics per era. Chart.js 4.4.6 is already loaded via the `_base.html` CDN script. Implement as a small horizontal bar with each era's count, clickable bars filtering the list below. **Skip this for v1** unless implementation is straightforward — the grouped list is the primary view.

### Nav integration

Add `Timeline` to the nav links in `app/templates/_base.html` between `Library` and `Series`. The template already uses a `for href, label in [...]` loop for the nav so this is a one-line addition.

### Stats page bonus

The existing `/stats` page (`app/routers/stats.py`) already has an `Era` donut driven by `Comic.era`. No change needed there — we lean on the new parser only when /timeline needs structured years.

---

## Critical files

| File | Action |
|---|---|
| `app/services/timeline.py` | **NEW** — `parse_timeline_value`, `derive_era_bucket`, era-bucket ordered list constant. |
| `app/routers/timeline.py` | **NEW** — `GET /timeline` route; loads owned SW comics, parses, groups, renders. |
| `app/templates/timeline.html` | **NEW** — extends `_base.html`; filter sidebar + grouped list. |
| `app/main.py` | Register the timeline router. |
| `app/templates/_base.html` | Add `Timeline` nav link. |
| `app/tests/test_timeline.py` | **NEW** — unit tests for `parse_timeline_value` + `derive_era_bucket`; integration tests for the route (renders, canon filter, era filter, undated bucket, owned-only scope). |

## Helpers to reuse (don't reinvent)

- `app/routers/library.py::_query_page` filter pattern + facet helpers — clone the structure for `_query_timeline` (but simpler: no pagination needed for a chronological list).
- `app/routers/library.py::_drop_empty` query-list cleaner.
- `app/services/duplicates.py::_format_icon` — same icon mapping for the row format chip; either import or copy the 6-line helper.
- `app/templates/_base.html` filter-drawer JS hooks (`lbFiltersToggle`, etc.) — already global, no change needed.
- `app/services/series_progress.py::compute_progress` — NOT used here; the timeline page doesn't need progress, just chronological listing.

## Verification

1. **Unit tests**: `parse_timeline_value` handles
   - `"3956 BBY"` → `(-3956, -3956, "3956 BBY")`
   - `"3964–3962 BBY"` → `(-3964, -3962, "3964–3962 BBY")` (en-dash AND hyphen variants)
   - `"22 BBY"` → `(-22, -22, "22 BBY")`
   - `"5 ABY"` → `(5, 5, "5 ABY")`
   - `"Imperial era"`, `""`, `None` → `(None, None, raw)`

2. **Integration tests**: save a handful of comics with mock timeline values, hit `/timeline`, assert:
   - Comics appear in the expected era panels.
   - `?canon=legends` filters out canon entries.
   - Undated bucket renders when at least one owned SW comic has `timeline=None`.
   - Non-Star-Wars-fandom comics don't show up.
   - Unowned comics (no Copy) don't show up.

3. **Live smoke** on the user's existing library:
   - `docker compose up -d --build` then visit `/timeline`.
   - Expect Old Republic era to cluster the KotOR omnibus + EC volumes at `3964–3962 BBY`.
   - Expect Rebellion era to gather Star Wars (1977) and similar at `0 BBY` / `1 ABY`.
   - Confirm the Canon / Legends / Both toggle works.

4. **Tests pass**: `docker build --target test -t longbox-test . && docker run --rm longbox-test pytest -q` — all green, including the new `test_timeline.py`.

## Out of scope

- Per-story timeline granularity (a TPB's collected_issues spans many years; we use the TPB's single `Comic.timeline` value).
- Editing `Comic.timeline` from the timeline page — already editable on `/comic/{id}/edit`.
- Cross-fandom timelines (Marvel comics with their own chronology) — not needed for the user's library.
- Inferring `timeline` from each issue's article when the omnibus's value is missing — out of scope; the user can refresh the comic to re-pull.
