# Screenshots

UI captures of the live app, referenced from the user-facing docs.

## Conventions

- **Filename**: `<page>-<device>.png` — e.g. `library-desktop.png`,
  `add-mobile.png`, `comic-detail-variants.png`.
- **Format**: PNG preferred. JPEG acceptable for big shots where
  compression won't muddy text.
- **Width**: desktop shots at 1280–1440 px wide; mobile at 390 px
  (iPhone) or 360 px (Android small).
- **Don't include personal data.** Use a demo library if a shot would
  expose your collection's contents or personal storage labels.

## Current captures

| Filename | What it shows |
|---|---|
| `home-desktop.png` | Landing page hero + recent additions + series progress |
| `library-desktop.png` | Filtered grid with the sidebar open |
| `series-desktop.png` | Collage covers + completion bars |
| `stats-desktop.png` | Composition donuts |
| `admin-desktop.png` | Admin hub (backup / restore / export / cleanup / danger zone) |
| `comic-detail-desktop.png` | Full comic detail page |
| `missing-desktop.png` | Owned-series gap report |
| `duplicates-desktop.png` | Redundantly-held issues |
| `add-desktop.png` | The `/add` lookup form |
| `home-mobile.png` | Mobile home |
| `library-mobile.png` | Mobile library card grid |
| `comic-detail-mobile.png` | Mobile comic detail |

## How to re-take

The captures were produced via a one-off Playwright script driving a
headless Chromium against the live deployment at 1440×900 desktop and
390×844 mobile. To re-take after UI changes, the easiest path is:

1. Bring up the stack (`docker compose up --build`) and add some comics
   so the views aren't empty.
2. Open each page in a fresh browser at the target viewport.
3. DevTools → ⋮ menu → "Capture screenshot" produces a clean PNG you
   can drop in here.

If you keep the script around (don't commit it), it's easy to re-batch
all 12 in one command — see git history for an example.
