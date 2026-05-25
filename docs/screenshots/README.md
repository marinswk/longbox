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

## Suggested shots to add

| Filename | What it shows |
|---|---|
| `home-desktop.png` | Landing page hero + nav |
| `library-desktop.png` | Filtered grid with the sidebar open |
| `library-mobile.png` | Same view with the bottom-sheet filter drawer |
| `add-confirm-variants.png` | `/add` confirm with the variant cover strip visible |
| `comic-detail.png` | Comic detail page with copies, tags, series links |
| `series-detail.png` | Series detail with progress + missing-issues list |
| `stats.png` | Stats page with donuts |
| `admin.png` | Admin hub |
| `scanner-mobile.png` | Fullscreen barcode scanner with corner brackets |

## How to take one

The screenshots are taken from a real deployment; reproducing them
yourself works the same way:

1. Bring up the stack (`docker compose up --build`).
2. Add a few comics for visual interest.
3. Open the browser at the target page, choose a clean viewport, and
   capture (DevTools → "Capture screenshot" works for full-page PNGs).
