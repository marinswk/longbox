# Longbox roadmap

Living document. Currently **329 passing tests**. Cross items off as they
land; add new ones at the bottom of the relevant section.

```bash
# Run tests
docker build --target test -t longbox-test .
docker run --rm longbox-test
```

---

## What's left

### Medium effort

- [ ] **ComicVine list importer.** Paste a CV list URL → fan out and add
      all. Reuses the CSV-import wizard from step 4 onwards.

### Bigger projects

- [ ] **Auth.** Single-user password (basic-auth) probably enough; multi-tenant
      would be a schema lift.
- [ ] **Read-only API tokens.** For Home Assistant / dashboard widgets.
- [ ] **Recommendation rail.** Publisher + era + tag overlap. Data-thin
      until libraries are large.

### Tech debt

- [ ] **Profile `compute_progress`.** Called per-series from `/` and `/stats`.
      Watch for N+1 once libraries grow past ~200 series. Deferred until
      there's real scale to measure against.
- [ ] **Stop bundling tests in runtime image.** `RUN rm -rf /app/app/tests`
      in the runtime stage is a workaround. Cleaner: split the build context
      so the runtime layer never sees them.

### Permanently out of scope (do not implement)

- ~~Wishlist~~ — dropped in migration `0004`. Do not reintroduce.
- ~~Loan tracking~~ — dropped in migration `0003`.
- ~~Pull list~~ — dropped in migration `0004`.
- ~~Cost / value / spend KPIs~~ — explicitly removed earlier.
- ~~Reading the actual files (CBZ/CBR/PDF)~~ — Longbox is a catalog,
  not a reader.

---

## Done

### v1 polish pass

- [x] Dockerfile `test` target — `docker build --target test -t longbox-test .`
      runs the whole suite cleanly.

### v1.1 batch (high value, low effort)

- [x] Tag pages (`GET /tags` index + `GET /tag/{name}` redirect).
- [x] Library filters for `read_status` / `storage`.
- [x] Clickable stats donut slices → library filter.
- [x] CSV export (flat one-row-per-copy file).
- [x] Reading log at `/reading-log`, grouped by month.
- [x] Quick-mark-read button on comic detail.
- [x] MetadataCache age prune in lifespan startup.
- [x] Themed 404 / 500 pages.
- [x] Bulk edit on `/library` (floating action bar).
- [x] Series cover collage on series detail.
- [x] Library card height consistency (fixed-height info slot).
- [x] Bulk-edit checkbox polish (top-right, semi-transparent).
- [x] Auto-tagging on add (CV + Metron + Wookieepedia characters / arcs /
      concepts), retro-fill via `POST /comic/{id}/auto-tag`.
- [x] Edit-form completeness (format, language, canon, era, timeline,
      collected_issues, publisher, series).
- [x] Bulk tag add/remove.

### Series + fandom rework

- [x] **Fandom moved from Series → Comic.** Migration `0006`. Fandom picker
      widget at add + edit time. Filter chip on `/library`. Donut on `/stats`.
- [x] **Series-dedup backfill** at lifespan startup — merges rows with the
      same normalized name into one canonical row.
- [x] **Multi-line name stripping** in the Wookieepedia parser + lifespan
      backfill for legacy rows.
- [x] **Format normalization** at every write site + lifespan backfill.

### Wrong-pick triage workflow

- [x] **`/series` browse page** — collage covers, status filter (complete /
      in progress / untracked), sort by name / count / completion.
- [x] **`/comic/{id}/repick`** — manual re-search with custom query, source
      checkboxes, candidate picker. Apply force-overwrites every source-derived
      field and reassigns series; auto-prunes the previous (now-empty) series.
- [x] **`/admin/inconsistencies`** — liberal sweep flagging suspect data
      (prose `collected_issues`, format-vs-source mismatches, outlier years).
      One-click Review buttons jump straight into the re-pick page.
- [x] Refresh-from-source uses the same `apply_repick` pipeline so refresh
      and re-pick can never drift apart on what counts as source-owned.
- [x] Re-pick & refresh clear `cover_url_local` so the new cover renders
      immediately, before the background download finishes.

### Polish layers

- [x] **`collected_issues` smart rendering** — entries that look like clean
      article titles become wikilinks; "COLLECTING:" prose stays plain text.
- [x] **CSV import wizard** — full 5-step flow at `/admin/import/csv`:
      upload → map → configure → resolve → commit. With:
      - Pre-confirmed pick + horizontal pager on multi-hit cards
      - Per-row freeform search box
      - Cancel-import button
      - Source-aware aggregator (only queries selected sources)
- [x] **CSV roundtrip** — empty template at `/admin/import/csv/template`
      plus a re-importable library export at
      `/admin/import/csv/export-library`.
- [x] **Filter sidebar redesign** — collapsible `<details>` per facet, tag
      search box, year sorted desc, lower-case storage + title-case display.
- [x] **Admin Danger Zone** — factory reset with typed-confirmation phrase.
- [x] **`/api/export/csv`** flat CSV download for spreadsheet workflows.

### Mobile pass

- [x] **Phase 1 — quick fixes.** Form inputs ≥16px (no iOS focus-zoom).
      Hamburger nav drawer. Filter bottom-sheet drawer on `/library` and
      `/series`. ≥44px touch targets. Safe-area-inset padding on bottom
      bars. Dismissable flash banners. Display-font scaled down on small
      screens.
- [x] **Phase 2 — layout polish.** Comic detail: small cover next to a
      column of actions; secondary buttons hidden behind a mobile-only
      disclosure. COPIES table renders as stacked cards on `<sm` via the
      `lb-stack-mobile` responsive-table pattern. Candidate-card `source_id`
      truncation with hover-title.
- [x] **Phase 3 — PWA.** SVG icons (default + maskable). Dynamic
      `/manifest.webmanifest`. Service worker at `/sw.js` (network-first
      HTML, cache-first covers). Apple touch icon + iOS web-app metas.
      Install button captures `beforeinstallprompt` and reveals itself
      when the browser is ready.
- [x] **Phase 4 — scanner.** Fullscreen barcode scanner overlay with
      crawl-yellow corner brackets, torch toggle (where supported via
      `MediaStreamTrack.getCapabilities`), haptic feedback on hit,
      Escape-to-close. Better error messages for HTTPS / permission /
      camera failures.
