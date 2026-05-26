# Admin page

`/admin` is the data-management hub. A sticky pill-strip sub-nav at the
top jumps to anchored sections:

📦 Backup · ⏪ Restore · 📤 Export · 📥 Import · 🧹 Cleanup · ☢ Danger zone

## Backup

**📦 Full backup (.zip)** — JSON dump of every row + every cover image
file under one archive. Restorable on a fresh deployment to reproduce
the library exactly.

**Show row counts** — opens `/api/export/preview`, a small JSON of the
counts per table. Useful for verifying the backup will contain what you
expect before downloading.

## Restore

Drop a `.zip` (full backup) or `.json` (data only) into the form. Hits
`/admin/import` which:

1. Validates the JSON schema-version (accepts v2, v3, v4 — v4 added
   the `ComicSeries` + `ComicContainment` link tables so a backup
   round-trips multi-series memberships and containment relationships;
   v2 backups have their unused `Series.fandom` field silently
   stripped before import)
2. Truncates every user-data table inside a single SQL transaction
3. Re-inserts every row + writes cover files back to disk
4. Commits

A failure mid-way leaves the original data untouched (single
transaction). The form has an `hx-confirm` so a stray click can't
trigger the destructive flow.

## Export

Three CSV / JSON export options:

- **Data only (.json)** — `/api/export` — every row in JSON form, no
  cover images. Smaller than the full backup; not re-importable round-
  trip without using `/admin/import`.
- **CSV (one row per copy)** — `/api/export/csv` — flat denormalized
  CSV. Joins Comic + Series + Publisher and emits a row per Copy. For
  spreadsheets. **Not re-importable via the wizard** — the column shape
  is different.
- **CSV (re-importable)** — `/admin/import/csv/export-library` — one
  row per Comic with the canonical wizard header. Round-trips through
  `/admin/import/csv`.

## Import (CSV wizard)

Full guide: [`import-csv.md`](import-csv.md).

The admin section just has a **📥 Start CSV import →** button + a note
mentioning `/add` for single-comic flows.

## Cleanup

Three tools:

**🧹 Prune orphan series** — deletes every Series row that no Comic
points at anymore. Usually a no-op (the app auto-prunes on delete +
re-pick) but exists for paranoia.

**🧽 Clean up library** — heavy pass that walks every comic and:

- re-derives the canonical source / series for any comic whose
  `source` is set,
- re-runs the inferred-series linkage so omnibuses pick up newly-
  recognised contained series,
- prunes empty series rows that the per-comic refresh left behind.

Long-running — progress polls `/library/cleanup/status` and renders an
HTMX status line. Safe to leave running in another tab.

**🩺 Find inconsistencies** — read-only sweep that flags comics whose
data shape disagrees with itself. Each result has a one-click
**🔁 Review →** button straight into `/comic/{id}/repick`. Heuristics:

| Flag | Detection |
|---|---|
| `prose_collects` | `collected_issues` matches `^COLLECTING:` or contains a comma |
| `format_collects_mismatch` | `collected_issues` is set AND `format` isn't a trade type |
| `single_issue_pattern_with_trade_format` | `format` is a trade AND `source_id` ends with a small integer (and no Vol./Volume marker) |
| `cover_date_year_mismatch` | `cover_date.year` is more than 5 years off the median for the series (only fires for series with ≥5 dated comics) |

The sweep is **liberal** — false positives are cheap, missed cases are
not. Each flag is a suggestion, not an automatic fix.

## Danger zone

**☢ Factory reset** — empties every user-data table. The schema /
alembic state stays put so the app keeps running without a restart.

Three deliberate clicks before destruction:
1. Click **I understand — show the wipe form** (hidden behind a
   disclosure)
2. Type `WIPE EVERYTHING` exactly into the confirm input
3. Submit → JS confirm dialog → final commit

Optional checkbox: also delete every cover image file from disk.

**No undo.** Take a full backup first via the Backup section if you
might want this back.

What gets wiped:
- `Comic`, `Copy`, `Series`, `Publisher`
- `Creator`, `Character`, `StoryArc`, `Tag`
- Every join table
- `ImportSession`, `ImportRow`
- `MetadataCache`

What stays:
- The schema itself (tables aren't dropped)
- `alembic_version`
- The `covers/` directory (only its files are deleted)

After wipe → success message with **Browse library** + **Run another
import** buttons.
