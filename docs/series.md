# Series

A **Series** in Longbox is a parent row that owns:
- a `name` (string, free-form, normalized for dedup)
- an optional `publisher_id`
- optional `source` + `source_id` for refreshing the expected-issues list
- optional `expected_issues` — newline-joined article titles, used to
  compute owned-vs-missing completion progress

Every Comic optionally points at a Series via `series_id` (nullable, so
one-shots without a parent series are allowed).

## `/series` browse page

Top-nav **Series**. Card grid of every series in your collection with:

- **Cover collage**: up to 4 owned-comic covers per series in a 2×2
  grid. 1 cover fills the tile, 2 split horizontally, 3 use one big +
  two small, 4+ fall back to the clean 2×2.
- **Name** (2-line clamp)
- **Publisher** + **Fandom** badge (fandom is derived from the most
  common `Comic.fandom` across the series's comics)
- **Status** — complete / in progress / untracked
- **Progress** — `owned / expected` + percentage when an issue list
  exists; otherwise `N comics · untracked`

Same filter sidebar / drawer pattern as `/library`. Sort options: name
A→Z, name Z→A, most comics first, most complete first.

## `/series/{id}` detail page

Top: series name + publisher + progress bar.

Then:

- **COVERS** — clickable poster grid of every owned comic in the
  series. Auto-hidden if you don't own anything yet.
- **ISSUES** — the expected list with per-issue owned / missing
  indicators. Owned issues link to their `/comic/{id}` page; missing
  issues are shown as plain text.
- **REFRESH FROM SOURCE** — form to (re)pull the issue list from
  Wookieepedia / ComicVine / Metron. The series's source + source_id
  get persisted so the next refresh is one click.
- **MERGE** — single dropdown of every other series. Picking a target +
  submit reassigns every comic in this series to the target, copies any
  missing source/source_id/issue-list data to the target if it was
  empty, and deletes this series row. Useful when you have legitimate
  same-name series with different publishers; Longbox now dedups
  same-name rows automatically at startup, but this UI lets you collapse
  edge cases by hand.

## Source-aware refresh

The refresh form has three source options:
- **Wookieepedia** — pass the article title (e.g.
  `Star Wars: Knights of the Old Republic`).
- **ComicVine** — pass the numeric volume id (the number in
  `/volume/4050-NN/` URLs).
- **Metron** — pass the numeric series id.

Each fetcher returns the list of expected issue article titles. The
detail page recomputes owned-vs-missing against the comics in that
series. Owned comics match via:

1. **Direct hit**: `Comic.source_id == expected article title`
2. **Trade credit**: the comic's `collected_issues` mentions the
   expected article
3. **Trailing digits fallback**: trailing integer of the article title
   matches `Comic.issue_number`

That third fallback is for legacy comics that pre-date source linking.

## Series-level dedup

The `_get_or_create_series` helper deduplicates by normalized name
(lowercase, hyphen + whitespace folded), so:

- `Star Wars: Jedi Knights` + Marvel Comics
- `Star Wars: Jedi Knights` + Marvel Worldwide, Incorporated
- `star wars: jedi knights` + (no publisher)

…all collapse to a single row. The dedup probe runs every time a comic
is saved or moved.

A **lifespan backfill** (`backfill_merge_duplicate_series`) also runs
on every cold start to clean up any historical duplicates created
before the dedup probe existed. Idempotent — safe across restarts.

## Auto-prune

When a comic is deleted (or moved to a different series via re-pick /
edit), the previous series is auto-pruned if it just lost its last
comic. The same logic lives in:

- `apply_repick` (re-pick / refresh flows)
- the comic delete endpoint

There's also a manual one-shot **🧹 Prune orphan series** button on
the admin Cleanup section for paranoid sweeps.
