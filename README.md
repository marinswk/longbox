# Longbox

Self-hosted comic library manager. Catalog issues and trades by ISBN, UPC, or
upstream IDs; pull metadata from Wookieepedia, ComicVine, Metron, and Open
Library; browse with filters, stats, and a comic-book-themed UI. Runs as a
single Docker container.

**Stack:** FastAPI · SQLModel + SQLite (aiosqlite) · Alembic · HTMX + Tailwind
(CDN). No Node/JS build step; one Python process.

---

## Feature highlights

| Area | What you get |
|---|---|
| **Add** | ISBN / UPC / ComicVine ID / Metron ID lookup. Free-text search across every source in parallel. Per-comic re-pick if the auto-match is wrong. Camera-based barcode scanner. |
| **Library browse** | Card grid with filters: publisher, series, year, fandom, format, continuity, era, tag, story arc, read status, storage. Search. Group by. Bulk edit (storage / format / fandom / canon / era / mark-read / tag add/remove). Click a stats slice → land on the matching filtered library. |
| **Series view** | `/series` index with collage covers, completion bars, status (complete / in progress / untracked). Series detail page shows progress, owned vs. missing issues, refresh-from-source button. |
| **Stats** | KPI strip, composition donuts (fandom / format / continuity / era), physical copies donuts (read status / condition / storage), 12-month activity bars (added / read), highlights (oldest, most recent, heaviest add-month, most-tagged). All donut slices are clickable. |
| **Tags** | Free-form tagging. Tag index at `/tags`. Auto-tagging on add from upstream characters / story arcs. |
| **CSV import** | Five-step wizard: upload → map columns → choose sources → resolve rows (search + multi-hit picker + custom-query) → commit. Round-trippable CSV template. |
| **Cleanup** | Admin sweep that flags suspect data (wrong-pick comics, prose collected_issues, format vs source mismatch, outlier years). One-click jump into the per-comic re-pick flow. |
| **Portability** | Full backup `.zip` (data + covers). JSON-only export. Re-importable round-trip CSV. Restore endpoint. **Factory-reset wipe** behind a typed confirmation phrase. |
| **Mobile** | Responsive layout. Hamburger nav. Filter bottom-sheet drawer. Fullscreen barcode scanner with corner brackets, torch toggle, haptic feedback. PWA install (web manifest + service worker + offline shell). |
| **Reading log** | Timeline of read copies grouped by month. |

Total: ~329 passing tests as of this writing. Tests live in `app/tests/` and
run via the `test` Docker target.

---

## Deploy

### Portainer / git-pull stack (production)

1. **Stacks → Add stack → Repository**.
2. Repository URL: this repo. Authenticate if private.
3. Compose path: `docker-compose.yml`.
4. Environment variables: paste contents of `.env.example`. Uncomment any
   `COMICVINE_API_KEY` / `METRON_USER` etc. you have. Nothing is required —
   the app degrades gracefully when a source is unconfigured.
5. Deploy.
6. App reachable at `http://<host>:8080/`. Health: `/health`.

LAN-only by design. No reverse proxy or TLS in the stack. The named volume
`longbox_data` persists the SQLite DB and downloaded covers across redeploys.

> **Note on the camera scanner:** browsers require a secure context for
> `getUserMedia`. `http://localhost` counts as secure; `http://192.168.x.y`
> does **not**. If you want to scan from another device on your LAN, front
> the container with a reverse proxy that terminates TLS (Caddy / Traefik /
> nginx) and visit via `https://`.

### Local Docker

```bash
cp .env.example .env
docker compose up --build
```

App at `http://localhost:8080/`. SQLite + covers in the named volume.

### Local Python (dev)

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run uvicorn app.main:create_app --factory --reload --port 8000
```

`http://localhost:8000/`. Data goes into `./data/` by default (configurable
via `DATA_DIR`).

---

## Run the test suite

A dedicated Docker stage runs every test with all dev deps installed:

```bash
docker build --target test -t longbox-test .
docker run --rm longbox-test
```

No host Python needed. The runtime image is unaffected — `pytest` and the
test files only live in the `test` stage.

For a single test or a quick iteration during development:

```bash
docker run --rm longbox-test pytest -q app/tests/test_pwa.py
```

---

## Configuration (.env keys)

Every key is optional except `DATA_DIR` (defaults to `/data`).

| Key | Default | Purpose |
|---|---|---|
| `APP_ENV` | `production` | Just for logs / banners. |
| `DATA_DIR` | `/data` | Where SQLite + cover image files live. The volume mount lands here. |
| `COMICVINE_API_KEY` | unset | Enables the ComicVine source. Get a free key at comicvine.gamespot.com. |
| `COMICVINE_USER_AGENT` | `Longbox/0.1` | CV requires a non-default UA. |
| `METRON_USER` / `METRON_PASS` | unset | Enables the Metron source. Free account at metron.cloud. |
| `METADATA_CACHE_TTL_DAYS` | `30` | Upstream lookups are cached this long. Lifespan prune deletes older rows on every cold start. |

Wookieepedia and Open Library need no credentials.

---

## Documentation

User-facing guides for each feature live in [`docs/`](docs/):

- **[docs/quickstart.md](docs/quickstart.md)** — first-run flow, getting your
  first comic in.
- **[docs/adding-comics.md](docs/adding-comics.md)** — ISBN/UPC lookup, ID
  lookup, free-text search, camera scanner, manual entry, re-pick when the
  auto-match was wrong.
- **[docs/library.md](docs/library.md)** — browsing, filters, sort, bulk
  edit, group by.
- **[docs/series.md](docs/series.md)** — series index, completion tracking,
  refresh from source, merge.
- **[docs/tags-and-fandoms.md](docs/tags-and-fandoms.md)** — manual tags vs.
  auto-tags, fandoms (which differ from tags), tag pages.
- **[docs/import-csv.md](docs/import-csv.md)** — the five-step wizard,
  expected CSV shape, round-trip with the export.
- **[docs/admin.md](docs/admin.md)** — backup / restore / export / inconsistencies
  sweep / orphan prune / factory reset.
- **[docs/mobile-and-pwa.md](docs/mobile-and-pwa.md)** — install as an app,
  the barcode scanner, what works offline.
- **[docs/architecture.md](docs/architecture.md)** — high-level layout for
  developers / contributors.

---

## Project layout (quick map)

```
app/
  main.py            FastAPI factory + lifespan migrations & backfills
  config.py          pydantic-settings env loader
  db.py              async SQLite session / engine
  models.py          SQLModel tables
  migrations.py      Alembic runner
  routers/           one file per top-level URL family
  services/          source clients (CV, Metron, Wookieepedia, OL),
                     aggregator, CSV import, repick, wipe, etc.
  templates/         Jinja2 + HTMX + Tailwind. Inline JS where useful.
  static/            tiny static assets (SVG icons)
  tests/             pytest suite
alembic/             migration files (numbered, 0001 → 0007)
docs/                user guides
Dockerfile           multi-stage: builder → test → runtime
docker-compose.yml   single-container production deploy
ROADMAP.md           living backlog
CLAUDE.md            project context for future AI-pair-coding sessions
```

---

## License

Single-user, self-hosted. No license declared yet — treat as
all-rights-reserved until otherwise noted.
