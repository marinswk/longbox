# Longbox — Self-Hosted Comic Library Manager

> **Historical design document (April 2026).** Preserved for context;
> the README, ROADMAP, and `docs/` directory are the current source of
> truth. Most decisions below shipped — see [ROADMAP.md](ROADMAP.md)
> for the actual delivery history.
>
> A self-hosted web app to catalog a personal comic book collection. Scan ISBNs and issue IDs, fetch metadata from open APIs, and browse your library with filters, stats, and a quirky comic-book-themed UI.
>
> Deployed via Docker Compose on a Portainer host.

---

## 1. Goals & non-goals

### Goals
- Catalog comics by **ISBN** (trade paperbacks, GNs, omnibuses) and by **issue ID** (single issues — Marvel, Dark Horse, IDW, etc.).
- Pull as much metadata as possible (title, cover, series, publisher, year, creators, characters, story arcs, page count).
- When multiple sources/covers exist, **let the user pick**.
- Track **copies owned** and **price paid in EUR** (optional; falls back to cover price).
- Persist data across rebuilds and reinstalls (Docker volume).
- Library view with **thumbnails, filters, grouping** (series, fandom, publisher, year).
- Stats page.
- Modern UI with a **quirky comic-book feel** (halftones, panel borders, speech bubbles, sound-effect flourishes — used tastefully so it doesn't get tiring).

### Non-goals (at least for v1)
- Reading the actual comic files (CBZ/CBR/PDF). This is a *catalog*, not a reader.
- Multi-user accounts. Single-user / single-household for v1.
- Cloud sync. Self-hosted only.

---

## 2. Recommended tech stack

| Layer       | Choice                                       | Why                                                                                                           |
| ----------- | -------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Backend     | **Python 3.13 + FastAPI**                    | Async (essential for fan-out API calls), automatic OpenAPI docs, modern, fast.                                |
| ORM         | **SQLModel** (SQLAlchemy 2.0 + Pydantic)     | Single model definition for DB and API. Easy with FastAPI.                                                    |
| DB          | **SQLite** (via aiosqlite)                   | As requested. Plenty for a personal collection up to ~100k rows.                                              |
| Migrations  | **Alembic**                                  | So schema changes don't lose data on upgrade.                                                                 |
| HTTP client | **httpx** (async)                            | For talking to ComicVine / Metron / Marvel / Open Library in parallel.                                        |
| Frontend    | **HTMX + Alpine.js + Tailwind CSS**          | Server-rendered pages with snappy partial updates. One container instead of two. Tailwind handles the quirky theme cleanly. |
| Templates   | **Jinja2**                                   | Native to FastAPI.                                                                                            |
| Barcode scanning | **html5-qrcode** (browser-side)         | Webcam ISBN scanning on phone & laptop.                                                                       |
| Package mgr | **uv**                                       | Fast, modern Python packaging.                                                                                |
| Container   | **Multi-stage Dockerfile + docker-compose**  | One image, one service, one volume. Portainer-friendly.                                                       |

### Why HTMX over React/Next.js for this project
- Single container = simpler Portainer stack.
- Server-rendered = better for SEO-irrelevant private apps and faster first paint.
- Less ceremony for a solo project; you spend time on features, not on bundlers.
- Tailwind + a few CSS tricks (halftone backgrounds, comic fonts, panel borders) deliver the quirky look regardless of framework.

If you'd rather have a SPA: **SvelteKit** is a clean alternative, with FastAPI as a pure JSON API. It adds a second container.

---

## 3. Comic metadata API strategy

### Sources (current implementation)

| Source            | Used for                                                          | Auth         | Notes                                                                                       |
| ----------------- | ----------------------------------------------------------------- | ------------ | ------------------------------------------------------------------------------------------- |
| **Wookieepedia** | **ISBN** (Star Wars trades) + **UPC** (single-issue barcodes)      | None         | MediaWiki Action API at `https://starwars.fandom.com/api.php`. Search by ISBN/UPC text → parse `{{ComicCollection}}` (trades) or `{{ComicBook}}` (issues) infobox → resolve cover via `imageinfo`. Best-in-class for SW; returns nothing for non-SW comics (graceful). |
| **Open Library** | ISBN (everything else)                                             | None         | `GET /api/books?bibkeys=ISBN:<isbn>&format=json&jscmd=data`. Covers via Covers API.         |
| **ComicVine**    | Issue-ID lookups (preferred); series-issue lists                   | Free API key | `GET /issue/4000-<id>/` and `GET /volume/4050-<id>/?field_list=issues`. 200/hr rate limit. **No UPC search** — full-text search doesn't index barcodes. |
| **Metron**       | Issue-ID lookups + **UPC** (`?upc=` filter) + series-issue lists    | Free account | `GET /api/issue/<id>/`, `/api/issue/?upc=<upc>`, paginated `/api/issue/?series=<id>`. **Has no public ISBN filter** — `?isbn=` is silently dropped, so Metron stays out of the ISBN flow. |

> **Why Metron is not in the ISBN flow:** Probed live (Apr 2026) — every Metron list endpoint silently drops `?isbn=`, returning the full default page. `/api/collection/` exists but is the user's own personal library, not a public ISBN index. ISBN lookups via Metron simply aren't possible.

> **Marvel Comics API:** Permanently retired in November 2025 — `developer.marvel.com` 301-redirects to `marvel.com`. Not a viable source.

> **Coverage gaps to revisit later** (non-SW trades that Open Library doesn't have):
> 1. **Google Books** as a second ISBN source (`https://www.googleapis.com/books/v1/volumes?q=isbn:<isbn>`). Free 1 000/day quota with an API key; the unauthenticated quota is shared and frequently exhausted.
> 2. **GCD (Grand Comics Database) datapack** — weekly MySQL dump under CC-BY at `https://www.comics.org/download/`. Best comic-specific coverage and the only fully-offline option, but adds an import pipeline. Their REST API requires OAuth and the public site is Cloudflare-shielded, so the datapack is the only practical access mode.

### Lookup flow
1. **User scans/enters an identifier.**
2. **Detect type:**
   - 13 digits starting with `978`/`979` → ISBN-13.
   - 10 digits → ISBN-10.
   - 12 digits, or 13–18 digits not starting with 978/979 → UPC (single-issue barcode, possibly with variant suffix).
   - anything else → issue-ID (ComicVine / Metron internal IDs).
3. **ISBN flow:** Wookieepedia + Open Library in parallel. Wookieepedia first in the result list (richer SW data; returns nothing for non-SW so OL fills in).
4. **UPC flow:** Metron + Wookieepedia in parallel. Metron's `?upc=` filter handles non-SW comics it knows about; Wookieepedia full-text search picks up Star Wars titles. The full UPC is tried first; if Metron returns nothing, the 12-digit prefix is tried as a fallback (some records store the bare UPC-A without the variant suffix). ComicVine has no UPC search and is intentionally skipped.
5. **Issue-ID flow:** ComicVine + Metron in parallel — ComicVine first.
6. **Aggregate results.** If one match → confirm screen. If multiple → "Pick one" screen with cover thumbnails and key metadata side by side.
7. **On confirm:** dedupe-check by ISBN-13 / ISBN-10 / ComicVine ID / Metron ID; if a match exists, prompt for adding another `Copy` instead of creating a duplicate `Comic`.
8. **Ask for price paid** (optional, prefilled blank). If left blank, store cover price as fallback for stats.
9. **Cache the API response** so re-scans don't hit rate limits.

### Caching layer
- Table `metadata_cache(source, key, payload_json, fetched_at)` with a TTL of 30 days.
- All API clients write through this cache.
- Cover images are downloaded once and stored in `/data/covers/<hash>.jpg` so the app keeps working if the upstream source goes away.

### Secrets
- API keys in `.env` (`COMICVINE_API_KEY`, `METRON_USER`, `METRON_PASS`, `MARVEL_PUBLIC_KEY`, `MARVEL_PRIVATE_KEY`, `GOOGLE_BOOKS_KEY`).
- All sources are optional at runtime — the app should gracefully degrade if a key is missing.

---

## 4. Data model (high-level)

```
Publisher  (id, name, slug)
Series     (id, name, publisher_id, start_year, end_year, fandom, description, cover_url)
Comic      (id, series_id, issue_number, variant, title, cover_date, page_count,
            isbn_10, isbn_13, comicvine_id, metron_id, marvel_id,
            cover_url_local, cover_url_remote, description,
            cover_price_eur, created_at, updated_at)
Copy       (id, comic_id, condition, storage_location, price_paid_eur, purchase_date,
            notes, read_status, date_read, lent_to, lent_on)
Creator    (id, name, role)              # writer, artist, colorist, etc.
ComicCreator(comic_id, creator_id, role)
Character  (id, name)
ComicCharacter(comic_id, character_id)
StoryArc   (id, name)
ComicArc   (comic_id, arc_id)
Tag        (id, name)
ComicTag   (comic_id, tag_id)
Wishlist   (id, comic_id OR free-text label, added_at, notes)
PullList   (id, series_id, started_at, active)
MetadataCache(id, source, key, payload, fetched_at)
```

A `Comic` is a unique edition; a `Copy` is one physical book you own. `copies_owned` for a comic = `count(Copy where comic_id = X)`. This is the cleanest way to handle "I own three copies of Saga Vol. 1" and to track per-copy data (one might be signed, another lent out).

`fandom` on `Series` is a free-text or tag field (e.g. "Star Wars", "Marvel Universe", "Hellboy"). Auto-populate from publisher + a small map for big franchises; let the user override.

---

## 5. Feature list — full

### v1 (MVP)
- [ ] Add comic by ISBN (typed or webcam-scanned)
- [ ] Add comic by issue ID (ComicVine ID, Metron ID, Marvel ID)
- [ ] Multi-source aggregation + "pick one" disambiguation
- [ ] Duplicate detection → "you already own N copies" prompt + add another copy
- [ ] Optional price-paid input (EUR), cover-price fallback
- [ ] Library grid view with cover thumbnails
- [ ] Filters: publisher, series, fandom, year, read status, condition, tag
- [ ] Group views: by series, by fandom, by publisher, by year
- [ ] Comic detail page with all metadata + per-copy list
- [ ] Edit any field manually
- [ ] Stats page
- [ ] Manual entry fallback (no API match)
- [ ] Cover image upload fallback
- [ ] Comic-themed UI (halftone, panel borders, sound-effect microinteractions)

### v1.1 quality-of-life
- [ ] Read status & reading log
- [ ] Condition grading
- [ ] Storage location
- [ ] Tags & per-comic notes
- [ ] CSV/JSON export
- [ ] CSV/JSON import (so you can restore or migrate)
- [ ] Search across everything

### v1.2 collection-power-user
- [ ] Missing-issues detection per series
- [ ] Duplicates view
- [ ] Series-completion progress bars

### v2 (later, optional)
- [ ] Multi-user accounts
- [ ] Public sharing of a read-only collection link
- [ ] OPDS feed (so reading apps can browse the catalog)
- [ ] Bulk add (paste a list of ISBNs)

---

## 6. Stats page — what to show

- Total comics, total copies, total unique series, total publishers
- Read vs. unread (donut)
- Condition distribution
- Heatmap: comics added per month (last 12 months)
- "You have read X comics this year"

Charts: Chart.js or similar — keep dependencies light.

---

## 7. UI / theme direction

Aim: **modern and quirky**, not childish. Think *"a museum gift shop's comic section"*, not *"clip-art Sunday paper"*.

- **Typography**: clean sans (Inter or similar) for body, a chunky display face (Bangers, Bowlby One) only for headings and accents. Avoid Comic Sans.
- **Color**: white/off-white background, ink-black text, a single bold accent (Spider-red, Hulk-green, or Wonder-Woman-gold — pick one).
- **Texture**: subtle halftone-dot pattern as a background or behind cards.
- **Cards**: thick black borders, slight rotation on hover (1–2°), soft drop shadow that looks like a panel coming off the page.
- **Microinteractions**: a small "POW!" / "KAPOW!" / "ZOK!" speech bubble animation on add-to-collection, finish-reading, or stat reveal. Use sparingly so it's a delight, not noise.
- **Empty states**: a single illustrated panel with a quip ("Your longbox is empty. Time to scan!").
- **Mobile-first**: scanning an ISBN with a phone is the primary input flow.

Tailwind + a small custom CSS file gets you 90% of the look.

---

## 8. Architecture

```
                       ┌─────────────────────────────┐
                       │    User (browser / phone)   │
                       └──────────────┬──────────────┘
                                      │ HTTPS
                                      ▼
                ┌─────────────────────────────────────────┐
                │  longbox-web container                  │
                │  ┌───────────────────────────────────┐  │
                │  │ FastAPI (uvicorn)                 │  │
                │  │  ├─ HTML routes (Jinja+HTMX)      │  │
                │  │  ├─ JSON API (/api/*)             │  │
                │  │  ├─ Background tasks              │  │
                │  │  └─ Static + covers               │  │
                │  └───────────────────────────────────┘  │
                │                                         │
                │  /data  (volume)                        │
                │   ├─ longbox.db   (SQLite)              │
                │   └─ covers/      (downloaded images)   │
                └──────────────┬──────────────────────────┘
                               │
                  outbound HTTPS to:
                  ComicVine · Metron · Marvel · Open Library · Google Books
```

Single container, single volume. Dead simple in Portainer.

---

## 9. Project layout

```
longbox/
├── docker-compose.yml
├── Dockerfile
├── .env.example
├── README.md
├── pyproject.toml
├── alembic.ini
├── alembic/
│   └── versions/
└── app/
    ├── main.py                 # FastAPI app factory
    ├── config.py               # pydantic-settings, reads .env
    ├── db.py                   # engine, session
    ├── models/                 # SQLModel models
    ├── schemas/                # API schemas (if separate from models)
    ├── routers/
    │   ├── pages.py            # HTML routes (HTMX)
    │   ├── library.py
    │   ├── lookup.py           # /api/lookup/isbn, /api/lookup/issue
    │   └── stats.py
    ├── services/
    │   ├── comicvine.py
    │   ├── metron.py
    │   ├── marvel.py
    │   ├── openlibrary.py
    │   ├── googlebooks.py
    │   ├── aggregator.py       # parallel fetch + merge
    │   ├── covers.py           # download + cache cover images
    │   └── stats.py
    ├── templates/              # Jinja2 (base.html, library.html, partials/)
    ├── static/
    │   ├── css/
    │   ├── js/
    │   └── covers/             # symlink or served from /data
    └── tests/
```

---

## 10. Docker / Portainer deployment

### `Dockerfile` (multi-stage, slim)
- Stage 1: install with `uv`, build wheels.
- Stage 2: `python:3.13-slim`, copy app, install deps, expose 8000, run uvicorn.

### `docker-compose.yml`
```yaml
services:
  longbox:
    image: longbox:latest
    build: .
    container_name: longbox
    restart: unless-stopped
    ports:
      - "8080:8000"
    env_file:
      - .env
    volumes:
      - longbox_data:/data
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 30s
      timeout: 5s
      retries: 3

volumes:
  longbox_data:
```

### Portainer
- Add as a **Stack** with this compose file.
- Set the env vars in the stack's environment section (ComicVine key, etc.).
- The named volume `longbox_data` is what makes data persist across reinstalls.
- For backups: snapshot the `longbox_data` volume, or expose `/health` and a `/api/export` endpoint that zips the DB + covers.

---

## 11. Build phases (for Claude Code on the PC)

Suggest tackling these in order — each is a self-contained chunk to give Claude Code.

1. **Bootstrap.** Repo, `pyproject.toml`, FastAPI hello-world, Dockerfile, compose, Portainer-deployable. Confirm `/health` responds inside the container.
2. **DB & models.** SQLModel models from §4, Alembic baseline migration, simple `/api/comics` CRUD.
3. **Open Library + Google Books.** ISBN lookup endpoint that returns merged candidates. Tests with a real ISBN.
4. **ComicVine + Metron + Marvel.** Issue-ID lookup endpoint. Aggregator service that merges by source.
5. **Covers service.** Download to `/data/covers`, serve from `/covers/<hash>.jpg`.
6. **Add-comic flow (HTML).** Form → lookup → "pick one" picker → confirm → save. Duplicate check → "you have N copies, add another?" prompt → optional price-paid.
7. **Library view.** Grid of thumbnails, infinite scroll or pagination, filter sidebar (publisher, series, fandom, year, read status), grouping toggle.
8. **Comic detail page.** All metadata, list of physical copies, edit, delete.
9. **Stats page.**
10. **Theme pass.** Tailwind config, halftone background, panel cards, sound-effect microinteractions, mobile polish.
11. **Webcam ISBN scanner.** html5-qrcode integration on the add page.
12. **Quality-of-life batch:** read status, condition, storage, tags, notes, search, export/import.
13. **Power-user batch:** missing-issues, wishlist, pull list, loans, duplicates, "what to read tonight", value totals.

Each phase is a clean prompt to Claude Code: *"Implement phase N from PLAN.md."*

---

## 12. Open questions to decide before phase 1

- [ ] Final name: **Longbox**? Something else?
- [ ] Single accent color: red / green / yellow / blue?
- [ ] Are you OK with HTMX + Tailwind, or do you want a SPA (SvelteKit / React)?
- [ ] Do you want me (Claude Code) to set up GitHub Actions for build/push of the Docker image, or local-build only?
- [ ] Public-internet exposure or LAN-only? (If exposed, you'll want a reverse proxy with HTTPS — Caddy or Traefik in another stack.)
- [ ] Default UI language: English, Italian, both?

---

## 13. Sanity-check checklist before shipping v1

- [ ] Wipe the volume, redeploy, restore from a JSON export → all data back.
- [ ] App runs with **zero** API keys configured (manual entry only must work).
- [ ] App runs with **only** Open Library configured (ISBN-only mode).
- [ ] Rate-limit hits on ComicVine/Metron degrade gracefully (cached results still served, friendly message on miss).
- [ ] All cover images load from `/data/covers/` even if the upstream URL 404s.
- [ ] Mobile webcam scanning works on iOS Safari and Android Chrome.
- [ ] Backup script (or `/api/export`) produces a single zip with DB + covers.
