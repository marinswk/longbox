# Architecture

High-level shape of the codebase for someone hopping in to add a
feature.

## Stack

- **FastAPI** (async) — every endpoint is `async def`. Lifespan startup
  runs Alembic migrations + a handful of idempotent backfills.
- **SQLModel + SQLite (aiosqlite)** — one file `data/longbox.db`. WAL
  mode. Each request gets its own `AsyncSession` via FastAPI dependency.
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
│
├── routers/            one file per top-level URL family
│   ├── home.py         GET /
│   ├── library.py      /library + grid HTMX endpoint + bulk-edit POST
│   ├── series.py       /series index + /series/{id} detail + merge
│   ├── detail.py       /comic/{id} + edit + repick + refresh + copies
│   ├── add.py          /add lookup, candidate save, search, save
│   ├── stats.py        /stats
│   ├── tags.py         /tags index + /tag/{name} + per-comic tag CRUD
│   ├── reading_log.py  /reading-log
│   ├── duplicates.py   /duplicates
│   ├── search.py       /search + /search/suggest (HTMX hint dropdown)
│   ├── imports.py      /admin/import/csv wizard (5 steps)
│   ├── admin.py        /admin hub + backup/restore/export/wipe
│   ├── lookup.py       (legacy lookup helpers, used by add.py)
│   ├── comics.py       /api/comics REST endpoints
│   └── pwa.py          /manifest.webmanifest + /sw.js
│
├── services/           pure logic, no FastAPI dependencies
│   ├── aggregator.py   parallel multi-source lookup + ranker
│   ├── cache.py        MetadataCache get_or_set + prune_expired
│   ├── collected_issues.py  link-vs-prose classifier for display
│   ├── comicvine.py    CV API client
│   ├── metron.py       Metron API client
│   ├── wookieepedia.py MediaWiki API client + infobox parser
│   ├── openlibrary.py  OL API client (ISBN-only)
│   ├── covers.py       cover image download + on-disk storage
│   ├── csv_import.py   CSV parser + autosuggest matcher
│   ├── import_commit.py CSV commit pipeline
│   ├── import_sources.py source-tile metadata for wizard step 3
│   ├── inconsistencies.py admin sweep heuristics
│   ├── repick.py       apply_repick (used by /repick AND /refresh)
│   ├── fandoms.py      Comic.fandom helpers + every lifespan backfill
│   ├── series_progress.py compute_progress + match_owned
│   ├── portability.py  full backup zip + JSON export/import
│   ├── wipe.py         factory reset
│   ├── schemas.py      LookupCandidate, CreatorRef pydantic models
│   └── errors.py       UpstreamRateLimit exception
│
├── templates/          Jinja2
│   ├── _base.html      layout shell, nav, drawer, mobile CSS,
│   │                   service worker registration
│   ├── partials/       reusable bits (cards, modals, drawers)
│   └── *.html          one file per top-level page
│
├── static/             tiny static assets (SVG icons only)
└── tests/              pytest suite (~300 tests)

alembic/versions/       0001 → 0007 sequential migrations
Dockerfile              multi-stage: builder → test → runtime
docker-compose.yml      single-container production deploy
```

## Request lifecycle

1. **Bootstrap (lifespan).** Alembic migrations run. Backfills sweep:
   metadata-cache prune, Wookieepedia fandom backfill, format
   normalization, multi-line name stripping, duplicate-series merge.
   All idempotent — every cold start is safe.
2. **Request.** FastAPI dependency injection hands the route an
   `AsyncSession`. Most routes are HTMX-aware: full-page render on
   navigation, partial swap on `HX-Request: true`.
3. **Upstream calls.** Routed through `services/aggregator.py`. Every
   external call goes through `cache.get_or_set(source, key, fetch)`
   which checks the `MetadataCache` table first and returns cached JSON
   on a hit. TTL configurable via `METADATA_CACHE_TTL_DAYS`.
4. **Templates.** Jinja2 + HTMX. Inline `<script>` blocks where useful
   (filter drawer toggle, bulk-edit state, candidate pager, scanner
   state machine). No external JS framework.
5. **Background tasks.** FastAPI `BackgroundTasks` for cover-image
   downloads after save / re-pick / refresh.

## Data model (mid-detail)

```
Publisher 1───* Series 1───* Comic 1───* Copy
                                │
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
- `Series.expected_issues` — newline-joined article titles. The
  missing-issues detector compares against `Comic.source_id` (direct
  match), `Comic.collected_issues` (trade credit), or trailing-digit
  fallback.
- `Comic.cover_url_local` vs `cover_url_remote` — local downloaded
  copy preferred for display; cleared on re-pick so the new remote
  shows immediately before the background download finishes.

## Source aggregator strategy

`find_candidates_multi(...)` in `app/services/aggregator.py`:

1. **ISBN/UPC fast path** — if present, hits exact-match endpoints on
   relevant sources only.
2. **Text search** — builds queries from `series + title`, falling
   back to `title` alone, then `series` alone.
3. **Filter** — drops candidates from un-selected sources (the user's
   choice in the import wizard or `/repick`).
4. **Dedup** — collapses repeats by `(source, source_id)`.
5. **Rank** — by year proximity (within tolerance), series-token
   overlap, source priority (Wookieepedia → CV → Metron → OL),
   cover-presence as tie-breaker.
6. **Return** — top N candidates + a list of rate-limited sources for
   the UI to surface as warnings.

Sources that the user didn't pick are **skipped at the network layer**
— no API call, no rate-limit warning surfaced. Both `lookup_full` and
`search_text` accept a `sources=` filter for this.

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
  `suggest_mapping`, etc.) — direct unit tests.
- **HTTP endpoints** — `TestClient(create_app())` + `client.get` /
  `client.post`. The lifespan runs every time, so migrations + backfills
  fire per-test.
- **Upstream APIs** — mocked via `respx` (httpx mocking). Real network
  calls never happen in tests.
- **Monkeypatching** — for `find_candidates_multi`, we replace it in
  `app.services.aggregator` to feed deterministic candidates.

The test DB lives at `data/test_longbox.db` (set by pytest fixtures or
env var). It accumulates rows across tests; tests that need to filter
their own seeded data use unique ISBNs (e.g. `9799400000401`) +
`/api/comics?limit=500` to avoid pagination flake.

## Lifespan startup chain

In order, every cold start runs:

```python
await run_migrations()                  # Alembic
await prune_expired()                   # MetadataCache TTL sweep
await backfill_wookieepedia_fandom()    # legacy SW comics → fandom
await backfill_normalize_format()       # lowercase Comic.format
await backfill_strip_multiline_names()  # legacy Series/Comic/Publisher
await backfill_merge_duplicate_series() # merge same-name rows
```

All idempotent. All cheap on a steady-state DB.

## Migrations

Numbered `0001` → `0007`. Run sequentially. Significant ones:

| # | What |
|---|---|
| 0001 | Initial schema |
| 0002 | Comic metadata expansion (upc, source, format, language, SW timeline) |
| 0003 | Drop Copy.lent_to, Copy.lent_on (loan tracking dropped from scope) |
| 0004 | Drop Wishlist + PullList tables (features dropped) |
| 0005 | Series.source + source_id + expected_issues (missing-issues detector) |
| 0006 | Move fandom from Series → Comic |
| 0007 | ImportSession + ImportRow tables (CSV wizard) |

Migrations under `0003` + `0004` permanently dropped features — see
`ROADMAP.md` for the "out of scope" list.
