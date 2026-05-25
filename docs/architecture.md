# Architecture

High-level shape of the codebase for someone hopping in to add a
feature.

## Stack

- **FastAPI** (async) — every endpoint is `async def`. Lifespan startup
  runs Alembic migrations + a chain of idempotent backfills.
- **SQLModel + SQLite (aiosqlite)** — one file `data/longbox.db`. WAL
  mode. Each request gets its own `AsyncSession` via FastAPI dependency.
  `NullPool` to keep cold-start predictable.
- **Alembic** — sequential migrations under `alembic/versions/`. The
  app runs them on startup via `app.migrations.run_migrations()`. No
  manual `alembic upgrade head` needed.
- **HTMX + Tailwind (CDN)** — no Node build. Templates ship as static
  Jinja2 files with `hx-*` attributes for partial swaps. Tailwind v3
  via the play CDN with a runtime config block in `_base.html`.
- **httpx** for upstream API clients (ComicVine, Metron, Wookieepedia,
  Open Library).

No JS framework, no SPA, no GraphQL, no auth, no Redis. The whole thing
runs in one Python process.

## Directory map

```
app/
├── main.py             FastAPI factory + lifespan migrations & backfills
├── config.py           pydantic-settings env loader
├── db.py               async SQLite session / engine
├── models.py           SQLModel tables
├── migrations.py       Alembic runner
├── version.py          single-source-of-truth semver (surfaced on /admin + /health)
│
├── routers/            one file per top-level URL family
│   ├── home.py         GET /
│   ├── library.py      /library + grid HTMX endpoint + bulk-edit POST + bulk-delete
│   ├── series.py       /series index + /series/{id} detail + refresh + merge + delete
│   ├── detail.py       /comic/{id} + edit + repick + refresh + copies CRUD +
│   │                    rederive-series + mark-read + delete + cover upload
│   ├── add.py          /add lookup + text search + confirm + save + _attach_inferred_series
│   ├── stats.py        /stats
│   ├── tags.py         /tags index + /tag/{name} + per-comic tag CRUD + auto-tag
│   ├── reading_log.py  /reading-log
│   ├── duplicates.py   /duplicates  (held-redundantly report)
│   ├── missing.py      /missing  (missing-issues + missing-trades across library)
│   ├── search.py       /search + /search/suggest (HTMX hint dropdown)
│   ├── imports.py      /admin/import/csv wizard (5 steps + cancel)
│   ├── admin.py        /admin hub + backup/restore/export/wipe + inconsistencies
│   ├── cleanup.py      /library/cleanup heavy-pass action + progress poller
│   ├── containment.py  /comic/{id}/contains  CRUD
│   ├── comic_series.py /comic/{id}/series  multi-series link CRUD
│   ├── lookup.py       /api/lookup (aggregator passthrough)
│   ├── comics.py       /api/comics REST endpoints
│   └── pwa.py          /manifest.webmanifest + /sw.js + icon PNG resizing
│
├── services/           pure logic, no FastAPI dependencies
│   ├── aggregator.py            parallel multi-source lookup + ranker
│   ├── cache.py                 MetadataCache get_or_set + prune_expired
│   ├── canon_index.py           Wookieepedia canon-list crawler index
│   ├── collected_issues.py      link-vs-prose classifier + coverage_titles + StoryCite
│   ├── comicvine.py             CV API client
│   ├── metron.py                Metron API client
│   ├── wookieepedia.py          MediaWiki API client + infobox parser
│   ├── openlibrary.py           OL API client (ISBN-only)
│   ├── covers.py                cover image download + on-disk storage
│   ├── csv_import.py            CSV parser + autosuggest matcher + translate_format
│   ├── import_commit.py         CSV commit pipeline
│   ├── import_sources.py        source-tile metadata for wizard step 3
│   ├── inconsistencies.py       admin sweep heuristics
│   ├── repick.py                apply_repick  (used by /repick AND /refresh)
│   ├── fandoms.py               Comic.fandom helpers + EVERY lifespan backfill
│   ├── series_progress.py       compute_progress + match_owned
│   ├── series_merge.py          merge_series (collapse two series rows into one)
│   ├── library_cleanup.py       heavy-pass library cleanup
│   ├── duplicates.py            redundant-ownership report builder
│   ├── portability.py           full backup zip + JSON export/import
│   ├── wipe.py                  factory reset
│   ├── icons.py                 PWA icon PNG generation
│   ├── schemas.py               LookupCandidate, CreatorRef pydantic models
│   └── errors.py                UpstreamRateLimit exception
│
├── templates/          Jinja2
│   ├── _base.html      layout shell, nav, drawer, mobile CSS,
│   │                   service worker registration
│   ├── partials/       reusable bits (cards, modals, drawers, picker tiles)
│   └── *.html          one file per top-level page
│
├── static/             tiny static assets (SVG icons only)
└── tests/              pytest suite (484 tests across 59 files)

alembic/versions/       0001 → 0011 sequential migrations
.githooks/pre-commit    prompt-injection marker scan (opt-in via core.hooksPath)
Dockerfile              multi-stage: builder → test → runtime
docker-compose.yml      single-container production deploy
```

## Request lifecycle

1. **Bootstrap (lifespan).** Alembic migrations run, then the backfill
   chain (see below). All idempotent — every cold start is safe.
2. **Request.** FastAPI dependency injection hands the route an
   `AsyncSession`. Most routes are HTMX-aware: full-page render on
   navigation, partial swap on `HX-Request: true`.
3. **Upstream calls.** Routed through `services/aggregator.py`. Every
   external call goes through `cache.get_or_set(source, key, fetch)`
   which checks the `MetadataCache` table first and returns cached JSON
   on a hit. TTL configurable via `METADATA_CACHE_TTL_DAYS`.
4. **Templates.** Jinja2 + HTMX. Inline `<script>` blocks where useful
   (filter drawer toggle, bulk-edit state, candidate pager, scanner
   state machine, variant picker). No external JS framework.
5. **Background tasks.** FastAPI `BackgroundTasks` for cover-image
   downloads after save / re-pick / refresh; collected-issues inference
   after save.

## Data model

```
Publisher 1───* Series 1───* Comic 1───* Copy
                                │
                                ├── 1───* ComicSeries ── *───1 Series   (multi-series membership)
                                ├── 1───* ComicContainment ── *───1 Comic (parent ↔ child)
                                ├── 1───* ComicCreator ── *───1 Creator
                                ├── 1───* ComicCharacter ── *───1 Character
                                ├── 1───* ComicArc ── *───1 StoryArc
                                └── 1───* ComicTag ── *───1 Tag

ImportSession 1───* ImportRow      (CSV wizard state)
MetadataCache                       (upstream response cache)
```

Notable fields:

- `Comic.fandom` — single string, lowercase. Lives on Comic (not
  Series) so one-shots and orphan comics still have one.
- `Comic.source` + `Comic.source_id` — provenance for refresh / re-pick.
- `Comic.series_id` — the PRIMARY series. `ComicSeries` link rows
  capture multi-membership; exactly one row per comic carries
  `is_primary=true` and matches `series_id`.
- `Comic.collected_issues` — newline-joined list of contained issue
  titles. Drives the trade-credit match in `series_progress.match_owned`
  and the inferred-series linkage in `_attach_inferred_series`.
- `Comic.cover_variants_json` — JSON list of `{label, url}` for variant
  covers, populated from the source's cover gallery at save / refresh.
  Used to populate the variant dropdown on the add-copy form.
- `Copy.variant_name` + `Copy.variant_cover_url` — which physical
  variant this copy ships with. NULL = standard cover.
- `Series.expected_issues` + `Series.canceled_issues` — the
  missing-issues detector compares against `Comic.source_id` (direct
  match), `Comic.collected_issues` (trade credit via
  `collected_issues.coverage_titles`), or trailing-digit fallback.
  Canceled issues are a sub-list shown separately and subtracted from
  the progress denominator.
- `Comic.cover_url_local` vs `cover_url_remote` — local downloaded
  copy preferred for display; cleared on re-pick so the new remote
  shows immediately before the background download finishes.

## Source aggregator strategy

`lookup_full` / `search_text` in `app/services/aggregator.py`:

1. **ISBN/UPC fast path** — if present, hits exact-match endpoints on
   relevant sources only.
2. **Text search** — fans out across every configured source in
   parallel via `_safe` (which converts `UpstreamRateLimit` exceptions
   into a per-source rate-limited notice without taking down the batch).
3. **Filter** — drops candidates from un-selected sources (the user's
   choice in the import wizard or `/repick`).
4. **Dedup** — collapses repeats by `(source, source_id)`.
5. **Rank** — by year proximity (within tolerance), series-token
   overlap, source priority (Wookieepedia → CV → Metron → OL),
   cover-presence as tie-breaker.
6. **Return** — `LookupResult` with `candidates` + `rate_limited` list
   for the UI to surface as warnings.

Sources that the user didn't pick are **skipped at the network layer**
— no API call, no rate-limit warning surfaced. Both `lookup_full` and
`search_text` accept a `sources=` filter for this.

## Lifespan startup chain

In order, every cold start runs:

```python
await run_migrations()                              # Alembic
await prune_expired()                               # MetadataCache TTL sweep
await backfill_wookieepedia_fandom()                # legacy SW comics → fandom
await backfill_normalize_format()                   # lowercase Comic.format
await backfill_splice_year_in_comic_titles()        # "Revelations 1" → "Revelations (2022) 1"
await backfill_single_issue_format()                # ComicBook-template imports default to single issue
await backfill_strip_multiline_names()              # legacy Series/Comic/Publisher
await backfill_merge_duplicate_series()             # merge same-name rows
await backfill_prune_dangling_comicseries()         # drop link rows pointing at gone comics
await backfill_comic_series_links()                 # mirror Comic.series_id into ComicSeries
await backfill_inferred_series_from_collected_issues()  # auto-link trades to underlying singles
await backfill_strip_umbrella_links_from_trades()   # drop bogus trade→umbrella links
await backfill_strip_bogus_movie_adaptation_links() # drop bogus Movie-Adaptations links
await backfill_prune_empty_inferred_series()        # sweep stale 0-issue rows
```

All idempotent. All cheap on a steady-state DB.

## Migrations

| # | What |
|---|---|
| 0001 | Initial schema |
| 0002 | Comic metadata expansion (upc, source, format, language, SW timeline) |
| 0003 | Drop Copy.lent_to, Copy.lent_on (loan tracking dropped from scope) |
| 0004 | Drop Wishlist + PullList tables (features dropped) |
| 0005 | Series.source + source_id + expected_issues (missing-issues detector) |
| 0006 | Move fandom from Series → Comic |
| 0007 | ImportSession + ImportRow tables (CSV wizard) |
| 0008 | ComicContainment table (parent comic contains child comics) |
| 0009 | ComicSeries link table (multi-series membership) |
| 0010 | Series.canceled_issues |
| 0011 | Variant covers (Comic.cover_variants_json + Copy.variant_name + .variant_cover_url) |

Migrations under `0003` + `0004` permanently dropped features — see
[`ROADMAP.md`](../ROADMAP.md) for the "out of scope" list.

## Testing

```bash
docker build --target test -t longbox-test .
docker run --rm longbox-test
```

The test stage installs dev deps (pytest + respx) and copies the test
directory in. The runtime stage explicitly deletes `app/tests` so the
production image stays lean.

Tests follow these patterns:

- **Pure functions** (`translate_format`, `parse_entries`,
  `suggest_mapping`, `_extract_cover_gallery`, etc.) — direct unit tests.
- **HTTP endpoints** — `TestClient(create_app())` + `client.get` /
  `client.post`. The lifespan runs every time, so migrations + backfills
  fire per-test.
- **Upstream APIs** — mocked via `respx` (httpx mocking). Real network
  calls never happen in tests.
- **Monkeypatching** — for `aggregator.lookup_full`, we replace it in
  `app.services.aggregator` to feed deterministic candidates.

### Known sharp edge

The shared test DB accumulates rows across tests; tests that need to
filter their own seeded data use unique ISBN/series prefixes
(`9799400000401`, `"BE Series A"`, etc.) AND pass
`/api/comics?limit=500` to avoid the default-limit-50 pagination gotcha.
See the inline comment in `test_detail.py::_save`.

## Prompt-injection guard

`app/tests/test_no_injection_markers.py` scans every source file for
known LLM-jailbreak phrases (the exact marker list lives in the test
file + the hook script). A matching `.githooks/pre-commit` hook runs
the same scan locally. Activate once per clone:

```bash
git config core.hooksPath .githooks
```

The test will catch any commit that slipped through without the hook,
so CI fails closed.

## App versioning

`app/version.py` exposes `__version__` as the single source of truth.
Surfaced on `/admin` (badge) and `/health` (JSON). Bumped per commit
following semver:

- **patch** — bug fixes, parser tweaks, small internal changes
- **minor** — a new user-facing feature
- **major** — a breaking or structural change
