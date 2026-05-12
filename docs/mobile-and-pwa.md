# Mobile & PWA

Longbox is a Progressive Web App. It installs on phones, runs without
browser chrome, caches HTML pages for offline use, and ships a
fullscreen barcode scanner.

## Install as an app

### Chrome / Edge / Brave / Samsung Internet (Android)

Open `http://<your-host>:8080/` on the phone's browser. After a few
seconds the **📱 Install Longbox** button appears next to the "Add a
comic" CTA on the home page (it's triggered by the browser firing
`beforeinstallprompt`).

Tap → native install dialog → installs to the home screen. Launching
the icon opens Longbox in its own window with no browser chrome.

### iOS Safari

iOS doesn't fire `beforeinstallprompt`, so the in-app button stays
hidden. Instead:

1. Open the site in Safari
2. Tap the Share button at the bottom
3. **Add to Home Screen**

iOS reads the web manifest (`/manifest.webmanifest`) for the name +
theme, and the `apple-touch-icon` link for the home-screen icon. Both
are wired up.

### What changes when installed?

- The app gets its own icon (crawl-yellow rounded square with a black
  "L" — `/static/icons/icon.svg`)
- No URL bar, no browser tabs
- `theme-color` sets the system status bar to the ink-blue header
- Recent HTML pages load offline (last cached version)
- Cover images load instantly (cache-first)

## Offline behavior

The service worker (`/sw.js`) implements:

| Resource | Strategy |
|---|---|
| `GET` HTML navigation | Network-first → cache fallback. Fresh content when online; last cached page when offline. |
| `/covers/*` | Cache-first. Once downloaded, the cover renders without a network round-trip. |
| `/api/*`, `/admin/*` | **Bypassed.** Never cached. JSON APIs would serve stale data; admin routes are sensitive. |
| `POST` / `PUT` / `DELETE` | **Bypassed.** Mutations always hit the network. |

If you open the app offline:
- The last visited page renders from the cache
- Cover images load from the persistent cache
- New page navigations may fail if you haven't visited them while online

## Mobile UI specifics

The whole app is responsive — every page works at 360px wide. Specific
mobile-only behaviors:

| Surface | Mobile behavior |
|---|---|
| Top nav | Collapses to a single ☰ button. Tap → right-slide drawer with all links + a search input. |
| `/library` + `/series` filters | The sidebar becomes a **🎛 Filters** FAB at the bottom-right. Tap → bottom-sheet drawer slides up from the bottom. Closes on backdrop tap or filter change. |
| Comic detail | Cover shrinks to 128px wide and sits next to the title. Secondary actions (Replace cover / Refresh / Delete) hide behind a "Cover & source" disclosure. |
| Copies table | Renders as stacked cards (one card per copy with labels). Powered by the global `lb-stack-mobile` CSS pattern. |
| Sticky bars | All fixed-bottom bars (bulk-edit, import-resolve footer) carry `pb-safe` so they don't sit on top of the iPhone home indicator. |
| Form inputs | Min 16px font-size on `<sm` so iOS Safari doesn't focus-zoom. |
| Headings | Display-font `text-3xl` / `text-5xl` / `text-7xl` scale down on `<sm` so they don't wrap awkwardly. |
| Tap targets | All `<button>`, `<select>`, `input[type=submit]`, etc. are min 44px tall on mobile. Anchors used as buttons (e.g. admin sub-nav) explicitly bumped via `min-h-11`. |
| Flash banners | Have an × dismiss button. |

## Barcode scanner

`/add` → **📷 Scan**.

The scanner opens as a fullscreen overlay covering the entire viewport.
Inside:

- Rear camera feed in a 4:3 aspect ratio frame
- **Crawl-yellow corner brackets** at the four corners as aim guides
- Status message below ("Aim at a barcode…")
- **💡 Light** button (only appears when the camera supports torch via
  `MediaStreamTrack.getCapabilities()`)
- **✕ Close** button

On a successful read:
- Haptic buzz (Android — iOS silently skips)
- ✓ Scanned: digits flashes
- Overlay auto-closes
- Lookup form auto-submits

### Supported barcode formats

| Format | Use case |
|---|---|
| EAN-13 | ISBN-13 — most TPBs / HCs / GNs |
| EAN-8 | rare; old short ISBNs |
| UPC-A | single-issue barcodes (12 digits, Marvel / DC / etc.) |
| UPC-E | rare compressed UPC |
| QR Code | not common on comics, but supported for completeness |

### Camera + HTTPS requirement

Browsers require a **secure context** for `getUserMedia` (camera access).

- `http://localhost` and `http://127.0.0.1` count as secure → dev works
- `http://192.168.x.y` does NOT → scanning from another device on your
  LAN won't work without TLS

To scan from a real phone over your LAN, put a TLS reverse proxy in
front of the container. Examples:

**Caddy (one-liner):**
```caddy
longbox.lan {
  reverse_proxy localhost:8080
  tls internal
}
```

**Traefik / nginx / Cloudflare Tunnel** all work too. Whatever gives
the phone an `https://` URL.

If TLS isn't an option, you can still type ISBNs into the form
manually.

### Why a fullscreen overlay

Inline-on-page scanners on mobile share the viewport with the rest of
the page, which means the camera preview ends up tiny on a phone in
landscape. Fullscreen makes the aim trivial and gives the corner
brackets enough room to feel like a real scanner app.

## Notes on iOS

- Camera + scan + service worker all work on iOS 13+ over HTTPS.
- `beforeinstallprompt` doesn't fire → the in-app install button stays
  hidden. The Add to Home Screen UX is Safari-native.
- `navigator.vibrate` is a no-op. The visual ✓ feedback covers the loss.
- The status bar respects `theme-color`. In Safari, it shows the
  ink-blue Longbox color.
