# Library

`/library` is the main browse view. Every comic in your collection
renders as a card with cover, title, series, publisher, copies count,
and either a series-progress bar (if the parent series has an issue list)
or its UPC.

## Filters

The sidebar (or bottom-sheet drawer on mobile, accessed via the **🎛
Filters** FAB) has these facets:

| Filter | Type | Notes |
|---|---|---|
| Search | substring | Matches `Comic.title` and `Series.name`. |
| Group by | dropdown | none / series / publisher / year. Affects grid grouping. |
| Fandom | multi-checkbox | Open by default since it's the highest-signal filter. |
| Publisher | multi-checkbox | |
| Series | dropdown | Single-select (long lists; usually you want one at a time). |
| Story arc | dropdown | Single-select. |
| Year | multi-checkbox | Sorted newest-first regardless of count. |
| Format | multi-checkbox | trade paperback, hardcover, omnibus, single issue, graphic novel. |
| Continuity | multi-checkbox | canon / legends. |
| Era | multi-checkbox | SW-specific (Imperial / New Republic / etc). |
| Tag | multi-checkbox + search | Has its own filter box because the list can be long. |
| Read status | multi-checkbox | read / reading / unread / dnf. |
| Storage | multi-checkbox | Free-form physical locations. |

Each section is a `<details>` element — collapses on click. Open by
default if any value in that facet is currently selected.

**Clear all** in the sidebar header drops every filter.

## Sort + pagination

Sort isn't a top-level control on `/library` — comics show newest first
by default (most recently added). Pagination via the prev/next buttons
at the bottom of the grid. 24 cards per page; bumpable via `?page_size=`
(capped at 100).

## Bulk edit

Each card has a small checkbox in its top-right corner. Tap any to
reveal a floating action bar at the bottom of the page:

```
[N selected] [Storage] [Format] [Continuity] [Era]
             [Add tags] [Remove tags] [☐ Mark first copy read]
                                              [Clear] [Apply]
```

What each field does:
- **Storage** — writes to every Copy of each selected comic.
- **Format / Continuity / Era** — overwrites `Comic.format` / `.canon` /
  `.era` on each selected comic.
- **Add tags** — comma- or semicolon-separated. Find-or-create each tag,
  link to every selected comic. Existing tags stay.
- **Remove tags** — same parsing; removes the named tags from every
  selected comic (if present).
- **Mark first copy read** — flips the first not-yet-read copy of each
  comic to `read_status=read, date_read=today`.

Empty fields are no-ops, so you can apply only Storage without
clobbering Format. Click **Apply** → POST → redirect back to whatever
URL you were on (filters preserved).

The checkbox selections survive HTMX grid swaps (filter changes,
pagination) within the same `/library` session.

## Group by

Setting **Group by = series** turns the flat grid into stacked
sub-grids, one per series, with the series name as a header above each
group. Same for publisher / year.

## URL syntax

Every filter is a query param. Examples:

```
/library?fandom=star+wars&format=trade+paperback
/library?read_status=read&year=2015&year=2024
/library?tag=favorites&group=series
```

Multi-select facets repeat the key (`?year=2015&year=2024`). Anything
that's a dropdown takes the single value.

## Stats integration

Slices on the stats donuts at `/stats` are clickable — clicking, say,
the "trade paperback" slice in the Format donut takes you to
`/library?format=trade+paperback`. Sentinel labels (`(unset)`,
`unknown`) are non-clickable.
