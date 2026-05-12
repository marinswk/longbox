# CSV import wizard

Five-step flow at `/admin/import/csv` that bulk-imports a CSV. The whole
thing is keyed by an opaque URL `token` so the user can leave mid-wizard
and come back.

## Step 1 — Upload

Drag-and-drop or pick a file. The parser:

- Decodes as UTF-8 (with BOM tolerance) → falls back to latin-1 on
  decode failure.
- Auto-detects separator (`,` / `\t` / `;`) by counting occurrences on
  the first non-empty line.
- Drops a **leading empty column** if the header has one (the Star Wars
  Canon CSV has this).
- Skips **section-header rows** (rows where only one cell is populated
  — typically just the Fandom column with a category like "Aggretsuko").
- Skips **empty rows**.

Max upload size: 10 MB.

Persists an `ImportSession` row + N `ImportRow` rows (one per parsed
data row) and redirects to step 2.

## Step 2 — Map columns

For each Longbox target field, a `<select>` populated with the CSV's
actual headers. **Auto-suggestion** matches:

| Target | Aliases |
|---|---|
| `series` | series, series name, seriesname, series_title |
| `title` | title, issue title, name |
| `issue_number` | issue number, issuenumber, issue, issue#, number |
| `year` | series year, seriesyear, year, release year |
| `publisher` | publisher, imprint |
| `format` | type, format, binding |
| `collected_issues` | collected issues, collects, contains |
| `variant` | variant, edition, cover variant |
| `fandom` | fandom, universe, franchise |
| `isbn_13` | isbn, isbn 13, isbn-13, isbn_13 |
| `upc` | upc, barcode, ean, sku |

Matching is **punctuation- and case-insensitive** (`ISBN-13` /
`isbn_13` / `ISBN 13` all hit). Each target's own label also counts as
an alias — so the canonical export's headers round-trip cleanly.

A **live preview** of the first 5 rows mapped to your selection renders
below the form (vanilla-JS rebuild on every `<select>` change, no
server round-trip).

Saving submits to `POST /map`, persists the JSON column-map on the
session, advances state to `config`.

## Step 3 — Configure sources

Four checkbox tiles:

| Source | Auto-on when |
|---|---|
| Wookieepedia | any row's `Fandom` mentions "star wars" |
| ComicVine | `COMICVINE_API_KEY` is configured |
| Metron | `METRON_USER`/`PASS` are configured |
| Open Library | the user mapped ISBN-13 or UPC AND any row has a value |

Tiles for unconfigured sources render greyed-out with a red badge.

Also on this page:
- **Year tolerance** slider (0–10, default 1). The aggregator's
  candidate ranker uses this for the `cover_date` proximity score.
- **Auto-tag from Fandom** toggle (default on). When on, the imported
  comic's `fandom` field comes from the CSV's mapped column.
- **Auto-tag from Publisher** toggle (default off — too coarse).

Revisit-friendly: if the user comes back to `/config`, the previous
selection is locked in (smart defaults only fire on first visit).

## Step 4 — Resolve

The big one. Renders one card per `ImportRow`. Each card has
`hx-trigger="revealed"` so its search only fires when scrolled into
view — that way a 250-row CSV doesn't fan out 250 simultaneous API
calls at page-load.

Per-row state machine:

| State | Meaning | Action buttons |
|---|---|---|
| `pending` | queued, searching | (spinner) |
| `matched` | exactly 1 hit, auto-picked | (pick visible, "skip this row") |
| `multi` | ≥2 hits, top auto-picked | ditto + horizontal pager through up to 50 candidates |
| `not_found` | 0 hits | freeform 🔎 search box + 🔄 retry + ✎ import as-is + skip |
| `as_is` | confirmed bare save | undo · search again |
| `skipped` | user dropped this row | undo · search again |
| `errored` | search exception or all sources rate-limited | error msg + 🔄 retry |

**Pre-confirmed pick**: multi-hit rows have the top-ranked candidate
auto-picked. The badge says `✓ pre-picked` and the row counts as
"ready" — no extra click required, but the user can override by tapping
a different candidate.

**Per-row search**: every card has a 🔎 search box that re-runs the
aggregator with a custom query, bypassing the CSV-derived series/title.
Useful when the auto-search returned the wrong thing or when nothing
was found.

**Horizontal pager**: when a row has >4 candidates, a `‹ prev / X / Y /
next ›` pager appears. Vanilla JS, paged 4 at a time. The currently-
picked candidate auto-reveals on its page when the card first renders.

### Sticky footer

At the bottom of the page:

```
142 / 250 ready    [142 matched] [18 multi] [12 not found] [4 skipped]
                                                     [✕ cancel] [🔄 search remaining] [Import 162 →]
```

- **Counts** update via OOB swap on every per-row state change.
- **🔄 search remaining** triggers all still-pending cards in batches
  of 6, 250ms apart. Polite to upstream APIs.
- **✕ cancel import** deletes the session + every child row, redirects
  to `/admin#import` with a flash banner.
- **Import N →** is disabled (`pointer-events-none opacity-50`) while
  any row is still `pending`. Errored rows do NOT block (the commit
  loop skips them cleanly).

## Step 5 — Commit

Pre-flight summary page:

```
220 will be imported with full upstream metadata (18 pre-picked from multiple hits)
18 will be imported as bare records (no metadata match)
12 skipped (4 explicit · 8 not found)
```

Submit triggers `commit_session()`, which iterates every committable
row in its own try/except so one bad row doesn't tank the batch.

Per row:
- Refetch candidate via the chosen source/source_id (skipped for as_is)
- Find-or-create publisher + series
- Create `Comic` with field-resolution priority **candidate → CSV → defaults**
- Set fandom from CSV's mapped column (if auto_tag_fandom is on);
  Wookieepedia hits fall back to `star wars`
- Run the standard creators + arcs + character-autotag chain
- Create an empty `Copy` (matches the regular add-flow behavior)
- Cover download queued as a `BackgroundTask`

Errored rows get `status="errored"` with the truncated exception
message; the done page lists them at the bottom for follow-up.

## Round-trip with the export

`/admin/import/csv/template` returns an **empty CSV** with the canonical
header. Fill it in your spreadsheet and re-upload via the wizard.

`/admin/import/csv/export-library` returns your library as a CSV with
the same canonical header. One row per Comic (not per Copy — the wizard
re-creates an empty Copy for each row anyway).

Both files use these headers:

```
Series · Title · Issue number · Series year · Publisher ·
Type / format · Collected issues · Variant · Fandom · ISBN-13 ·
UPC / barcode
```

Re-uploading either file lands every column on the right target field
automatically — the autosuggest matcher uses each target's label as an
implicit alias.

## Resume + cancel

The wizard URL contains a token. Bookmarking
`/admin/import/csv/{token}/resolve` lets the user come back days later
and pick up where they left off. Each step has its own GET handler that
re-renders the current state from the DB.

**Cancel** deletes the `ImportSession` + every `ImportRow` for that
session in one transaction.
