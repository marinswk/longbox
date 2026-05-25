# Longbox roadmap

Living document. Currently **484 passing tests** across 59 files. Cross items
off as they land; add new ones at the bottom of the relevant section.

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
- [ ] **Star Wars in-universe timeline page.** Planned design lives at
      [docs/plans/timeline-page.md](docs/plans/timeline-page.md). Not yet
      built; data is already on every comic via `Comic.timeline` /
      `Comic.era`.

### Bigger projects

- [ ] **Auth.** Single-user password (basic-auth) probably enough; multi-tenant
      would be a schema lift.
- [ ] **Public JSON API.** Today there's only a handful of ad-hoc `/api/*`
      endpoints (comics CRUD, export). A proper `/api/v1` surface with a
      shared bearer token would unlock scripting / Home Assistant widgets /
      a future mobile app.
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

### v1.1 batch — variant covers, parser fixes, public-release prep

- [x] **Variant cover tracking.** `Comic.cover_variants_json` caches the
      source's cover gallery; `Copy.variant_name` + `Copy.variant_cover_url`
      record which variant each physical copy ships with. Add flow shows a
      thumbnail strip; add-copy form has a dropdown + free-text override.
      Migration `0011`.
- [x] **{{Book}} infobox recognition.** Wookieepedia trilogy GNs use the
      generic `{{Book}}` template, which we now accept when categories
      prove the article is a graphic novel / TPB / omnibus.
- [x] **Movie-adaptation umbrella routing.** Trilogy GNs and individual
      "X Adaptation" miniseries route to a shared "Star Wars Movie
      Adaptations" series. Title-gated so tie-in one-shots that carry
      the category for thematic reasons aren't pulled in.
- [x] **Synthetic umbrella series no longer pollute trades.** TPBs that
      collect a one-shot stopped getting dragged into the One-shots /
      FCBD / Graphic Novels umbrellas. Backfill scrubs existing bogus
      links.
- [x] **Series-progress matcher hardening.** One owned comic can no
      longer satisfy multiple expected entries via the issue-number
      fallback. The host book of a multi-story anthology one-shot is
      no longer counted as owned from a partial-story TPB reprint.
- [x] **Year disambiguator in titles.** Comics whose source article
      title is `Revelations (2022) 1` are no longer saved as the
      indistinguishable `Revelations 1`. Backfill rewrites legacy rows.
- [x] **Single-issue format default.** Wookieepedia `{{ComicBook}}`
      infoboxes don't carry `media type=`; we now default to
      `single issue` for those and `graphic novel` for `{{GraphicNovel}}`.
- [x] **Empty-result cache shortening.** A negative ISBN/UPC lookup
      result is cached only briefly so adding a comic that wasn't on
      Wookieepedia yesterday but is today no longer waits 30 days.
- [x] **Prompt-injection guard.** A pytest test + a `.githooks/pre-commit`
      hook block source commits containing known injection markers.
- [x] **App versioning.** `app/version.py` is single-source semver.
      Surfaced on `/admin` and `/health`. Bumped per commit.
- [x] **Multi-series link table** (`ComicSeries`). Migration `0009`.
      Lets an omnibus belong to every series it collects.
- [x] **Comic containment** (`ComicContainment`). Migration `0008`.
      "What does this TPB contain" and "what owns this issue?" both
      render on the comic detail page.
- [x] **Canceled-issue tracking** (`Series.canceled_issues`).
      Migration `0010`. Series with planned-but-cancelled issues
      (e.g. Star Wars 3-D #4–7) no longer show forever-stuck progress.
- [x] **`/missing` index** — owned-series missing-issue + missing-TPB
      lists aggregated across the library.
- [x] **`/duplicates` index** — comics held redundantly (issue + a
      collected reprint, etc.).
- [x] **Library cleanup** — `/admin` "Clean up library" runs a heavy
      pass that re-derives series, re-runs inferred-series linkage, and
      prunes empties for every comic in one go. Per-comic refresh stays
      available for narrower fixes.

### v1 polish pass

- [x] Dockerfile `test` target — `docker build --target test -t longbox-test .`
      runs the whole suite cleanly.

### v1.1 batch (high value, low effort) — original

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
