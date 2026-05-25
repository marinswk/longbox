# Series

A **Series** in Longbox is a parent row that owns:
- a `name` (string, free-form, normalized for dedup)
- an optional `publisher_id`
- optional `source` + `source_id` for refreshing the expected-issues list
- optional `expected_issues` — newline-joined article titles, used to
  compute owned-vs-missing completion progress
- optional `canceled_issues` — a sub-list of `expected_issues` for
  issues that the wiki flags as cancelled (e.g. Star Wars 3-D #4–7).
  Rendered separately on the series page and subtracted from the
  progress denominator so a series with no PUBLISHED gaps reads as 100%.

Every Comic optionally points at a Series via `series_id` (nullable, so
one-shots without a parent series are allowed). It can also belong to
**additional** series via the `ComicSeries` link table — see
[multi-series links](#multi-series-links) below.

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
series — primary FK PLUS multi-series membership via `ComicSeries`.
Owned comics match in **two passes** so one physical comic can only
satisfy ONE expected entry as a single (the trade-credit pass is
independent and CAN serve many entries per trade):

**Pass A — direct source_id match.**
`Comic.source_id == expected article title`. Each matched comic is
consumed.

**Pass B — trailing-digit fallback** (only against unconsumed comics).
The trailing integer of the expected article title matches
`Comic.issue_number`. This handles legacy comics that pre-date source
linking and CSV-imported comics whose source_id was a guess.

**Trade credit (independent).** The comic's `collected_issues` covers
the expected article. This uses the `coverage_titles` set computed
from a comic's collected_issues blob, which includes story-half
attributions for combined `"Story (Book)"` entries — so a trade
collecting just one story from a multi-story anthology one-shot
credits the STORY, but not the host book. (Pre-fix bug; see
[ROADMAP.md](../ROADMAP.md).)

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

## Multi-series links

A Comic can belong to more than one Series at once via the
`ComicSeries` link table. Real examples:

- An **omnibus** collecting issues from KotOR singles AND KotOR: War
  lives in BOTH series so each `/series/{id}` reflects coverage.
- A **reprint TPB** belonging to Epic Collection AND the original
  ongoing series.
- An **event tie-in** showing up under the event series AND under the
  individual ongoing it was published in.

The Comic's `series_id` FK stays the PRIMARY series (one per row).
`ComicSeries` rows capture the extra memberships; exactly one row per
comic carries `is_primary=true` and matches `series_id`.

On the comic detail page the SERIES section lists every membership;
the primary is flagged. On `/comic/{id}` you can attach the comic to
another series via the **+ Add another series** form (live search of
existing series with a find-or-create fallback) and detach via the ✕
on a non-primary chip.

Most multi-series memberships get attached **automatically** when a
trade is saved:
`_attach_inferred_series` walks the comic's `collected_issues`, looks
up each contained issue article on Wookieepedia, and find-or-creates
+ links the canonical series each one belongs to. Synthetic umbrella
series (One-shots, FCBD, Graphic Novels — `source_id` LIKE
`Category:%`) are intentionally skipped here: the umbrella's primary
purpose is grouping standalone one-shots, not collecting every trade
that happens to reprint one.

## Containment

Separate from series membership: `ComicContainment` records "Comic
parent contains Comic child" — e.g. an omnibus comic contains its
constituent TPB comics, which in turn contain their issues. Each
relationship is a single link row with a `position` for ordering.

Both directions render on the comic detail page:
- **This collects** — child comics, with owned/not-owned dots so you
  can see at a glance whether your physical copies cover the whole
  contents.
- **Covered by** — parent comics in your library that list this one as
  a child.
