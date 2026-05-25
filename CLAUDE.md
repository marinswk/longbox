# CLAUDE.md — context for future AI-pair-coding sessions

This file is a project-orientation primer. Read it first when you
return to Longbox after time away.

---

## What this app is

Self-hosted comic-collection manager. Single Python container behind
Docker. SQLite + cover image files persisted in `/data`. No multi-user,
no auth, no cloud. LAN deploy via Portainer.

Currently **484 passing tests** across 59 files. Last shipped: variant
cover tracking (per-Copy), Wookieepedia parser polish (year disambiguator,
{{Book}} infobox, movie-adaptation umbrella routing), series-progress
matcher hardening (one comic → one expected entry; no host-book credit
from partial-story reprints), and public-release prep (LICENSE,
CONTRIBUTING, SECURITY).

---

## Stack — fixed decisions, don't re-litigate

- **FastAPI** with async-everywhere routes. Lifespan = Alembic migrations
  + idempotent backfills.
- **SQLModel + SQLite (aiosqlite).** One DB file, WAL mode. No
  PostgreSQL. No connection pool — `NullPool` to keep cold-start
  predictable.
- **Alembic** for migrations. Run sequentially on every startup via
  `app.migrations.run_migrations()`. Never expect a manual upgrade.
- **HTMX + Tailwind (CDN).** No Node, no build step. Inline `<script>`
  blocks where useful. No Vue / React / Alpine.
- **httpx** for outbound. Every upstream call goes through
  `app/services/cache.py::get_or_set` so the `MetadataCache` table is
  populated.
- **Pytest** for tests. `respx` for httpx mocking. No selenium.
- **uv** for Python dep management. `pyproject.toml` + `uv.lock`.
- **Docker** multi-stage build: `builder → test → runtime`. The
  `test` target runs the whole suite via `docker run --rm longbox-test`.

---

## How to do common things

### Run the suite (locally, no host Python)

```bash
docker build --target test -t longbox-test .
docker run --rm longbox-test
```

A single test file:

```bash
docker run --rm longbox-test pytest -q app/tests/test_pwa.py
```

### Restart the live container with the latest code

```bash
docker compose up -d --build
```

Healthcheck: `curl http://localhost:8080/health` (returns `{"status":"ok"}`).

### Inspect the live DB

```bash
docker exec longbox python -c "
import asyncio
from sqlmodel import select
from app.db import SessionLocal
from app.models import Series  # or whatever
async def main():
    async with SessionLocal() as s:
        rows = (await s.exec(select(Series).limit(10))).all()
        for r in rows:
            print(r)
asyncio.run(main())
"
```

### Add a migration

1. Add the table/column to `app/models.py`
2. New file under `alembic/versions/` numbered sequentially (`0008_...`)
3. Migration auto-runs on next startup via lifespan
4. If the change is "backfill existing rows", add a small async
   function to `app/services/fandoms.py` (yes the module name is a
   historical artifact; it's where every backfill lives) and call it
   from the lifespan list in `app/main.py`. Make it idempotent.

### Add a new top-level page

1. New router file in `app/routers/foo.py`
2. Import + `app.include_router(foo.router)` in `app/main.py`
3. New template `app/templates/foo.html` extending `_base.html`
4. Add a nav link in `_base.html` (desktop + mobile drawer both — DRY
   via the template's `for href, label in [...]` block)
5. Test file `app/tests/test_foo.py`

### Add a new metadata source

1. New module `app/services/yoursource.py` exposing `is_configured()`,
   `search_isbn(isbn)`, `search_upc(upc)`, `get_issue(id)`,
   `search_text(query, sources=None)` as needed.
2. Add to the parallel fan-out in `app/services/aggregator.py`:
   - `lookup_full(identifier, sources=None)` — ISBN/UPC/issue-id
     exact-match branch
   - `search_text(query, sources=None)` — free-text branch
   - Add the source key to `_SOURCE_PRIORITY` for ranking
3. Add to `app/services/import_sources.py::build_source_tiles` so the
   CSV wizard offers it as a checkbox tile.
4. Update `app/services/repick.py` if the source has special
   refresh/repick behavior (most don't).

---

## File-by-file what-lives-where

Quick map of where the meat is. See `docs/architecture.md` for the
full layout.

- **`app/main.py`** — `create_app()` factory + `lifespan` with the
  full backfill chain. Don't add long-running tasks here.
- **`app/routers/`** — one file per URL family. Routes named like
  `{action}_{noun}` (`comic_repick_apply`, `import_row_search`).
- **`app/services/aggregator.py`** — the multi-source dispatcher.
  Critical for understanding how lookups work.
- **`app/services/repick.py::apply_repick`** — the canonical "swap a
  comic's source + force-overwrite metadata + reassign series" flow.
  Both `/repick/apply` and `/refresh` go through this so they can't
  drift.
- **`app/services/fandoms.py`** — every lifespan backfill lives here
  (the name is historical; it predates the fandom-on-Comic migration).
- **`app/services/csv_import.py`** — CSV parser, autosuggest matcher,
  `translate_format()` (used everywhere, not just import).
- **`app/services/wipe.py`** — factory reset. Truncate order matters
  (FK constraints).
- **`app/templates/_base.html`** — global CSS rules (mobile font-size,
  safe-area utilities, hamburger drawer, filter drawer, responsive-
  table, display-font scaling). Almost every mobile concern lives here.

---

## Out-of-scope features (don't reintroduce!)

These were explicitly dropped. The migrations that removed them stay
for historical accuracy, but **never code them back in**:

- **Wishlist** — dropped in migration `0004`.
- **Loan tracking** — dropped in migration `0003` (Copy.lent_to,
  Copy.lent_on).
- **Pull list** — dropped in migration `0004`.
- **Cost / value / spend KPIs** — explicitly removed earlier.
- **Reading the actual files (CBZ/CBR/PDF)** — Longbox is a catalog,
  not a reader.

The `ROADMAP.md` "Permanently out of scope" section is the source of
truth for this.

---

## Conventions to follow

- **No `print()` in committed code.** Use logging if you need
  diagnostics. Tests can use `print` while debugging.
- **One commit per logical change.** When the user says "go" /
  "continue", commit only the work in the current phase.
- **Bump `app/version.py`** (`__version__`, semver) on every commit —
  patch for fixes, minor for features, major for breaking changes.
  Shown on `/admin` + `/health`.
- **Prompt-injection guard.** `app/tests/test_no_injection_markers.py`
  scans source for hidden-instruction phrases; a matching
  `.githooks/pre-commit` blocks such commits locally — activate once
  with `git config core.hooksPath .githooks`.
- **Tests are mandatory.** Every new endpoint or behavior gets a test
  file or extends one. Aim for the same density as existing files.
- **Templates use HTMX over JavaScript** whenever a partial swap will
  do. Inline JS only for state machines that can't be expressed as
  HTMX (scanner, filter drawer, bulk-edit selection).
- **CSS lives in `_base.html`** for cross-page concerns; per-template
  `<style>` blocks only when the styles really only apply to one page
  (e.g. library filter sidebar details/summary rotation).
- **Naming**: lowercase snake_case for fields. Display layer
  title-cases where appropriate (fandom, format, canon).
- **No new dependencies without a reason.** The stack is intentionally
  tiny. If you're tempted to add a package, check whether the existing
  primitives (HTMX, vanilla JS, Pillow-free SVG icons, etc.) can do it.

---

## Known sharp edges

- **The test DB accumulates across test runs.** Tests filter their own
  data by unique ISBN/series-name prefixes (`9799100000001`,
  `"BE Series A"`, etc.). When you add a test, use a unique prefix or
  the test will start failing in CI once the DB grows.
- **`/api/comics` defaults to limit=50.** Tests that find a specific
  comic by isbn must pass `params={"limit": 500}`. There are ~10
  legacy tests that follow this pattern.
- **Route order matters in FastAPI.** `/series/grid` is declared
  BEFORE `/series/{series_id}` in `app/routers/series.py` so the
  static path wins. If you add another `/series/{static-thing}` route,
  put it ahead of the typed-id route.
- **HTMX OOB swaps trip duplicate `id` issues.** The import-resolve
  page caught one of these once (the OOB-progress block was being
  emitted N times per page). Pattern fix: emit OOB blocks ONLY in
  swap responses, never in the initial page render. See the
  `oob_progress` flag in `app/routers/imports.py::_render_row_card`.
- **`cover_url_local` vs `cover_url_remote`.** Local wins for display.
  Always clear `cover_url_local` when changing `cover_url_remote` so
  the new image renders immediately, before the background download
  finishes. Pattern lives in `apply_repick` + `_backfill_metadata`.
- **Format / fandom normalization.** Always lowercase + collapse
  whitespace at write time via `translate_format()` /
  `app.services.fandoms.normalize()`. Title-case at display only.
- **Wookieepedia's `series=` infobox can be multi-value.** Use
  `_first_line()` (in `wookieepedia.py`) when reading
  single-valued fields. The lifespan backfill cleans legacy rows.

---

## Mobile pass — current state

All four phases shipped. The app:
- Renders at 360px wide cleanly
- Has a hamburger nav drawer
- Has bottom-sheet filter drawers on `/library` + `/series`
- Has dismissable flash banners + safe-area-inset
- Has a fullscreen barcode scanner with torch + haptic
- Installs as a PWA (manifest + service worker + apple-touch-icon)
- Works offline (last cached pages, immutable covers)

What's NOT mobile-optimized:
- Some less-trafficked pages (`/duplicates`, `/reading-log`) get the
  Phase 1 globals but no specific layout work. They're usable but not
  hand-tuned.
- The CSV wizard's resolve step has lots of dense UI; on phones it
  scrolls a lot. Acceptable for now.

---

## When in doubt

- Read `ROADMAP.md` for what's done and what's left.
- Read `docs/architecture.md` for the codebase layout.
- Read `docs/*.md` for per-feature user docs (also useful for
  understanding intent when refactoring).
- Run the tests before pushing. They're fast (~35s full suite).
- Don't add packages without checking with the human first.
