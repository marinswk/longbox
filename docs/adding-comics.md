# Adding comics

There are four ways to get a comic into Longbox, plus a re-pick flow for
when the auto-match was wrong. They all share the same upstream
aggregator under the hood.

## 1. Lookup by identifier

`/add` → the big input field.

- **ISBN** — 10 or 13 digits. Trades / GNs / omnibuses. Resolved via
  Wookieepedia (best for SW) + Open Library (everything else).
- **UPC** — 12 or 17–18 digits. Single-issue barcodes. Resolved via
  Metron (best for indies and Big Two) + Wookieepedia (SW articles often
  list the UPC). 17–18-digit UPCs include the variant suffix.
- **ComicVine / Metron issue ID** — pure integer. Resolved against the
  matching source.

Hit **Look up** → candidates from every responding source appear. Each
candidate card shows the cover, title, series, publisher, cover date, and
which source supplied it. Click **Save this one** → you land on the
candidate-picker confirm page with editable fields.

## 2. Free-text search

Below the lookup box: **Or search by title / series**. Type any
substring. Runs across Wookieepedia / ComicVine / Metron in parallel,
returns up to 60 hits paginated 12 at a time. Useful when you don't have
a number but know the title.

## 3. Barcode scanner

On `/add`, tap **📷 Scan**. The fullscreen overlay opens with the rear
camera + corner brackets aim guides. Aim at any EAN-13 (ISBN) or UPC-A
barcode. On a successful read:

- Haptic buzz (Android — iOS silently skips)
- ✓ Scanned: digits flashes on the overlay
- Overlay closes
- Lookup form auto-submits

Devices with a camera flash get a **💡 Light** toggle.

**HTTPS requirement.** Browsers need a secure context for camera access.
`http://localhost` counts as secure; `http://192.168.x.y` does **not**.
To scan from another device on your LAN, put a TLS reverse proxy
(Caddy / Traefik / nginx) in front of the container.

## 4. Manual entry (no source)

If none of the candidate hits are right, you can save with whatever
fields you typed:

1. Run any lookup (even an obviously-wrong one — just pick something to
   open the confirm page).
2. Edit every field on the confirm form.
3. Save.

For a totally-from-scratch comic, the cleanest path is to lookup
something that returns 0 hits → click the **Save with no source** path
(if surfaced) or fill the confirm form manually.

## The confirm page

After lookup + pick (or for manual entry), you land on a confirm page
with every Comic field editable:

| Field | Notes |
|---|---|
| Title, Issue number, Series, Publisher | Free-form. Series + publisher get find-or-create handling. |
| Variant | E.g. "1A", "1:25 Ratio", "Director's Cut". |
| Cover date | Full date or just a year. |
| Pages, Cover price (EUR) | Optional. |
| ISBN-13, ISBN-10, UPC | Stored as-is. |
| Cover URL (remote) | Auto-filled from source. The local image gets downloaded in the background after save. |
| Description | Multi-line. |
| **Fandom picker** | See below. |
| Price paid | Stored on the Copy, not the Comic. |

### The Fandom picker

Every comic optionally belongs to a **fandom** — `star wars`,
`aggretsuko`, `locke & key`, etc. Stored lowercase + whitespace-collapsed.

The picker has two inputs:
1. **Existing dropdown** — every fandom name currently in your library
   with its comic count.
2. **+ New fandom…** — selecting that option reveals a free-text input.
   Whatever you type wins over the dropdown.

For Wookieepedia hits the picker is pre-filled with `star wars`. For
other sources it defaults to empty (you can pick one or leave blank).

Why both: tags are free-form labels (`favorite`, `crossover`); fandom is
the universe membership and powers filter chips + stats donuts. They're
intentionally separate.

## Re-pick when the auto-match was wrong

The auto-pick gets it right most of the time, but not always — a common
miss is "user's CSV said it's a TPB; aggregator returned the
single-issue article". Fix this from the comic detail page:

1. `/comic/{id}` → click **🔁 Re-pick**.
2. Lands on `/comic/{id}/repick`, with a search box pre-seeded from
   the comic's current series + title + issue, and source checkboxes
   pre-ticked for whichever sources are configured.
3. **🔎 search** — runs the multi-field aggregator. Up to 50 candidates.
4. Pick the right candidate → confirm dialog → land back on `/comic/{id}`
   with refreshed metadata + a flash banner.

What gets overwritten on a re-pick:
- title, issue_number, cover_date, cover URL (and the cached local file
  is dropped — see below)
- description, format, language, canon, era, timeline, collected_issues,
  upc, page_count
- source / source_id (obviously)
- series, if the new candidate's series differs (find-or-create + the
  previous series gets auto-pruned if it just lost its last comic)
- creator / story-arc / character / fandom auto-tags are re-applied
  additively

What stays:
- tags you added manually
- copies + read status + condition + notes
- the user's `Comic.fandom` if set (unless they explicitly change it)

The **cover** updates the same way: the new remote URL is set
synchronously, `cover_url_local` is cleared, and the background task
downloads the new local file a moment later. The page re-renders the new
remote URL immediately — no need to wait or refresh.

## Refresh from source

If you didn't pick wrong but just want fresh upstream data (new cover
got published, description got rewritten, creators added), click
**↻ Refresh from {source}** on the comic detail. It uses the same
pipeline as re-pick under the hood, so refresh and re-pick can never
drift apart on what counts as source-owned.

## Re-running auto-tagging

The **✨ Auto-tag** button on the TAGS panel calls
`POST /comic/{id}/auto-tag`. Re-pulls characters / story arcs from the
comic's source and applies them as tags (`chars: name` for characters,
bare for arcs). Concepts are intentionally dropped — ComicVine's
`concept_credits` returns very noisy values.

If the comic has no `source` linked, the button shows a flash banner
telling you to either re-pick or refresh first.
