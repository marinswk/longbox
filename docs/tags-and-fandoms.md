# Tags vs Fandoms

Two related but distinct concepts.

## Fandom (`Comic.fandom`)

Single string per comic, free-form, lowercase + whitespace-collapsed at
write time. Examples: `star wars`, `aggretsuko`, `locke & key`,
`marvel 616`.

- Stored on the **Comic** (not the Series) so one-shots and orphan
  comics still have one.
- Title-cased for display: `star wars` → "Star Wars".
- Surfaced as:
  - A small badge on the comic detail page
  - A filter chip on `/library` (multi-select)
  - A donut on `/stats`
  - A facet on `/series` (computed as the mode across the series's
    comics)
- Set via the **Fandom picker** on add / edit / repick (existing
  dropdown + new-text-input).
- For Wookieepedia hits the picker is pre-filled with `star wars`.

## Tag (`Tag` + `ComicTag`)

Many-to-many labels per comic. Free-form, lowercase + whitespace-
collapsed, no length limit (but the tag-input UI caps at 40 chars).

- Stored as separate `Tag` rows and join rows in `ComicTag`.
- Surfaced as:
  - Linkified chips on the comic detail
  - Filter chip on `/library` (multi-select, with a built-in search
    box for long tag lists)
  - The `/tags` index page with counts
  - `GET /tag/{name}` redirects to the filtered library view
- Added via:
  - **Manual chip input** on the comic detail (TAGS panel)
  - **Auto-tag** on save — see below
  - **Bulk add tags** on the `/library` floating action bar

## Auto-tagging

When a comic is saved (or via the **✨ Auto-tag** retro-fill button on
the TAGS panel), upstream metadata is harvested:

| Source field | Stored as |
|---|---|
| Story arcs | bare tag (e.g. `war of the bounty hunters`) |
| Characters | prefixed `chars:` tag (e.g. `chars: han solo`) |
| Concepts (CV only) | **dropped** — CV's `concept_credits` is too noisy |

Caps per bucket: 10 arcs, 10 characters. Keeps a busy CV issue from
generating 50 tags.

Character names get **parenthetical disambiguation stripped**: CV often
returns `Han Solo (Earth-616)` — we store `chars: han solo`.

Wookieepedia parses the `==Appearances==` section of an article to pull
characters (it's not in the infobox). Up to 30 characters extracted;
later capped to 10 by the auto-tagger.

### Retro-fill via UI

On `/comic/{id}` → click **✨ Auto-tag** on the TAGS panel. This calls
`POST /comic/{id}/auto-tag`, which re-fetches the candidate via
`_refetch_candidate(source, source_id)` and applies the same logic.
Flash banner reports:
- `Added N tag(s) from <source>.` — happy path
- `No source linked — auto-tag needs the comic's original source.` — null source
- `<source> returned no characters or arcs to tag.` — upstream had nothing
- `Couldn't reach <source> for this comic — try again later.` — refetch failed

## Why both?

- **Fandom** is universe membership and stable identity. Used for
  high-signal filters and grouping. One value per comic, always.
- **Tag** is free-form labeling. `favorite`, `must-read`, `chars: Han
  Solo`, `gift-from-X`, `wishlist-in-progress`. Many values per comic.

They live in different tables, render with different UI, and serve
different mental models. Don't try to use one for both.

## CSV import

When the wizard's column-map step matches `Fandom` from the user's CSV,
the value is written to `Comic.fandom` directly (assuming `auto_tag_fandom`
is on in the configure step). Tags aren't typically in CSVs, but if the
user's CSV has a column we map to `tags`, each comma-separated value
gets find-or-created.
