# Contributing

Thanks for considering a contribution. Longbox is a small, opinionated
self-hosted app — read these notes before opening a PR so you don't
accidentally violate one of the project's intentional non-goals.

## Quick dev loop

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/marinswk/longbox.git
cd longbox
uv sync
cp .env.example .env       # optional; the app runs with no env set
uv run uvicorn app.main:create_app --factory --reload --port 8000
```

App at `http://localhost:8000/`. Data lands in `./data/`.

## Running the tests

The canonical way runs the suite in the same container the production
image is built from:

```bash
docker build --target test -t longbox-test .
docker run --rm longbox-test
```

Single file / quick iteration:

```bash
docker run --rm longbox-test pytest -q app/tests/test_pwa.py
```

A passing PR run is **required**. The full suite is ~3.5 minutes.

## Pre-commit hook (recommended)

A repo-local hook scans staged content for prompt-injection markers
(known LLM jailbreak phrases — see the marker list inside the hook
file) and blocks commits that match. Activate once per clone:

```bash
git config core.hooksPath .githooks
```

The same scan runs as `app/tests/test_no_injection_markers.py` so CI
catches it even without the hook installed.

## Project conventions

- **No JS framework.** HTMX over JavaScript wherever a partial swap
  will do. Inline `<script>` for state machines that can't be expressed
  as HTMX (scanner, filter drawer, bulk-edit selection, variant
  picker).
- **No new dependencies without a reason.** The stack is intentionally
  tiny (FastAPI, SQLModel, httpx, Pillow, Jinja2). Check whether HTMX
  + vanilla JS + a small helper can do it before adding a package.
- **Naming.** Lowercase snake_case for fields. Display layer
  title-cases where appropriate (fandom, format, canon).
- **Format normalization.** Always lowercase + collapse whitespace at
  write time via `translate_format()`. Title-case at display only.
- **Templates use HTMX over JavaScript** whenever a partial swap will
  do. Inline JS only for state machines that can't be expressed as
  HTMX.
- **No `print()` in committed code.** Use the standard `logging`
  module if you need diagnostics. Tests can use `print` during debug.
- **One commit per logical change.** When the user says "go" /
  "continue", commit only the work in the current phase.
- **Bump `app/version.py`** (`__version__`, semver) on every commit —
  patch for fixes, minor for features, major for breaking changes.
  Shown on `/admin` + `/health`.
- **Tests are mandatory.** Every new endpoint or behavior gets a test
  file or extends one. Aim for the same density as existing files.
- **CSS lives in `_base.html`** for cross-page concerns; per-template
  `<style>` blocks only when the styles really only apply to one page.

## Out of scope (don't reintroduce!)

These were explicitly dropped. The migrations that removed them stay
for historical accuracy, but **never code them back in**:

- **Wishlist** — dropped in migration `0004`.
- **Loan tracking** — dropped in migration `0003` (Copy.lent_to,
  Copy.lent_on).
- **Pull list** — dropped in migration `0004`.
- **Cost / value / spend KPIs** — explicitly removed earlier.
- **Reading the actual files (CBZ/CBR/PDF)** — Longbox is a catalog,
  not a reader.

The ROADMAP.md "Permanently out of scope" section is the source of
truth.

## Adding a migration

1. Add the table/column to `app/models.py`.
2. Create a new file under `alembic/versions/` numbered sequentially
   (`0012_...`).
3. The migration auto-runs on next startup via the FastAPI lifespan
   — no manual `alembic upgrade head` step.
4. If the change is "backfill existing rows", add a small async
   function to `app/services/fandoms.py` (yes, the module name is a
   historical artifact; it's where every backfill lives) and call it
   from the lifespan list in `app/main.py`. Make it idempotent.

## Adding a new metadata source

1. New module `app/services/yoursource.py` exposing `is_configured()`,
   `search_isbn(isbn)`, `search_upc(upc)`, `get_issue(id)`,
   `search_text(query, sources=None)` as needed.
2. Add to the parallel fan-out in `app/services/aggregator.py`:
   - `lookup_full(identifier, sources=None)` — ISBN/UPC/issue-id
     exact-match branch
   - `search_text(query, sources=None)` — free-text branch
   - Add the source key to `_SOURCE_PRIORITY` for ranking.
3. Add to `app/services/import_sources.py::build_source_tiles` so the
   CSV wizard offers it as a checkbox tile.
4. Update `app/services/repick.py` if the source has special
   refresh/repick behavior (most don't).

## Adding a new top-level page

1. New router file in `app/routers/foo.py`.
2. Import + `app.include_router(foo.router)` in `app/main.py`.
3. New template `app/templates/foo.html` extending `_base.html`.
4. Add a nav link in `_base.html` (desktop + mobile drawer both — DRY
   via the template's `for href, label in [...]` block).
5. Test file `app/tests/test_foo.py`.

## Branch protection

The `main` branch is protected by a ruleset (defined at
[`.github/rulesets/main-protection.json`](.github/rulesets/main-protection.json)).
What it enforces:

- No direct pushes to `main` — every change goes through a PR.
- The `pytest (Docker test stage)` status check must pass before merge.
- The PR branch must be up to date with `main` before merge (strict).
- Force pushes and branch deletion are blocked.
- The repository owner can bypass in emergencies (the JSON's
  `bypass_actors` block).

To re-apply the ruleset after a manual change or repo move:

```bash
gh api repos/marinswk/longbox/rulesets --method POST \
   --input .github/rulesets/main-protection.json
```

To preview the current state:

```bash
gh api repos/marinswk/longbox/rulesets
```

## Commit messages

Look at recent commits for the in-repo style. The basics:

- Imperative-mood first line, under ~70 chars.
- Blank line, then a paragraph or two of context: why this commit
  exists, what the user-facing effect is, anything subtle a future
  maintainer would want to know.
- For bug fixes, include a one-liner summary of the root cause.
- Add the `Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>`
  trailer if Claude Code wrote any of the patch — see CLAUDE.md.

## Known sharp edges to watch for

- **Test DB accumulates rows across test runs.** Use unique prefixes
  for any data your test seeds (`9799400000401`, `"BE Series A"`, etc.)
  and pass `params={"limit": 500}` when hitting `/api/comics` from a
  test (default is limit=50).
- **Route order matters.** `/series/grid` is declared BEFORE
  `/series/{series_id}` in `app/routers/series.py` so the static path
  wins. Add new static `/series/<thing>` routes ABOVE the typed-id route.
- **HTMX OOB swaps trip duplicate `id` issues.** Emit OOB blocks ONLY
  in swap responses, never in the initial page render. See the
  `oob_progress` flag in `app/routers/imports.py`.
- **`cover_url_local` vs `cover_url_remote`.** Local wins for display.
  Always clear `cover_url_local` when changing `cover_url_remote` so
  the new image renders immediately, before the background download
  finishes. Pattern lives in `apply_repick` + `_backfill_metadata`.
- **Wookieepedia's `series=` infobox can be multi-value.** Use
  `_first_line()` (in `wookieepedia.py`) when reading single-valued
  fields. The lifespan backfill cleans legacy rows.

For more, read [CLAUDE.md](CLAUDE.md) — it's written as orientation for
Claude Code sessions but is broadly useful to any contributor.
