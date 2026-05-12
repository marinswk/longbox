# Quickstart

5-minute guide to getting your first comic into Longbox.

## 1. Bring up the container

```bash
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8080/`. You'll see an empty hero screen with two
buttons.

## 2. Pick an API key (optional, but recommended)

Without any API keys, Longbox can still resolve:
- **Wookieepedia** for Star Wars comics (no key needed)
- **Open Library** for trade paperbacks with ISBNs (no key needed)

If you want broader coverage:
- Get a free **ComicVine** API key at <https://comicvine.gamespot.com/api/>.
  Set `COMICVINE_API_KEY` in `.env` and `docker compose up` again.
- Sign up for a free **Metron** account at <https://metron.cloud>. Set
  `METRON_USER` and `METRON_PASS`.

Each source is queried in parallel only if its credentials exist. The app
silently skips any source that's not configured.

## 3. Add your first comic

Three paths into `/add`:

**ISBN / UPC / Issue ID lookup.** Type or paste a number into the lookup
box and submit. Every configured source runs in parallel. Pick the best
candidate from the picker.

**Free-text search.** Click "Or search by title / series" and type
something. Across-source results, paginated.

**Barcode scanner.** On a mobile device, tap **📷 Scan**. A fullscreen
camera opens with corner brackets. Aim at any ISBN-13 / UPC barcode. The
scanner auto-submits on success. (Requires HTTPS or `localhost` — see
[mobile-and-pwa.md](mobile-and-pwa.md) for the camera/HTTPS rule.)

## 4. Browse what you've got

Top nav → **Library**. The default grid shows everything you own. The
filter sidebar (or bottom-sheet on mobile) lets you narrow by publisher,
series, year, fandom, format, continuity, era, tag, story arc, read
status, or storage.

## 5. Bulk-fill a library from a CSV

If you already have a spreadsheet of comics, head to **Admin → 📥 Import**.
The wizard walks you through column mapping, source selection, per-row
resolution (with a search box for misses), and a final commit step. See
[import-csv.md](import-csv.md) for the full guide.

## 6. Install on your phone

Open the site on your phone's browser → look for the **📱 Install Longbox**
button on the home page (Chrome / Edge / Brave / Samsung Internet). On
iOS Safari, use Share → Add to Home Screen. The app installs with its
own icon, opens without browser chrome, and keeps the last-visited pages
working offline. See [mobile-and-pwa.md](mobile-and-pwa.md).

---

Next reads:
- [adding-comics.md](adding-comics.md) — every add path in detail.
- [library.md](library.md) — filters, bulk edit, sort.
- [admin.md](admin.md) — backup, restore, factory reset, inconsistencies sweep.
