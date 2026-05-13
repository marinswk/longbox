"""PWA endpoints — web manifest + service worker + raster icons.

Manifest and SW are served dynamically (not as static files) so the
values stay in sync with the app theme. Icons are rendered from
Python (Pillow) so we can produce any pixel size on demand without
checking PNG binaries into git or bundling them in the image.

  GET /manifest.webmanifest         — PWA install metadata
  GET /sw.js                        — service worker JS (root-scoped)
  GET /icons/icon-{size}.png        — purpose=any icon PNG
  GET /icons/maskable-{size}.png    — purpose=maskable icon PNG

The service worker uses a network-first strategy for HTML navigation
(so a fresh deploy is picked up on the next page load) and a
cache-first strategy for cover images under `/covers/` (immutable
once downloaded — perfect cache candidates). POST/PUT/DELETE and
`/api/*` requests are passed through untouched.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.services.icons import render_icon

router = APIRouter(tags=["pwa"])


# Sizes referenced by the manifest + apple-touch-icon meta. We keep
# the list short on purpose — every entry costs the browser a fetch
# during install. 192 + 512 are the Chromium/Firefox minimums;
# 180 is what iOS Safari reads for the apple-touch-icon; 512 doubles
# as the launcher splash on Android.
_VALID_SIZES = {72, 96, 128, 144, 152, 180, 192, 256, 384, 512}


@router.get("/icons/icon-{size}.png")
async def icon_png(size: int) -> Response:
    """Return the `purpose=any` icon rendered as PNG at `{size}` px.
    Browsers fetch one of these per manifest entry on install."""
    if size not in _VALID_SIZES:
        raise HTTPException(status_code=404, detail="unsupported icon size")
    png = render_icon("any", size)
    return Response(
        content=png,
        media_type="image/png",
        # Icons are immutable for the life of a deployment. Long cache
        # keeps the install flow snappy; bumping the manifest URL is
        # the escape hatch if the icon ever changes.
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/icons/maskable-{size}.png")
async def maskable_png(size: int) -> Response:
    """Return the `purpose=maskable` icon rendered as PNG at `{size}` px.
    Android's adaptive-icon framework clips this with circular,
    rounded-square, etc. masks."""
    if size not in _VALID_SIZES:
        raise HTTPException(status_code=404, detail="unsupported icon size")
    png = render_icon("maskable", size)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/manifest.webmanifest")
async def manifest() -> JSONResponse:
    """Web App Manifest. Browsers that support PWA install (Chrome,
    Edge, Samsung Internet, Brave, iOS Safari 16.4+) read this to
    populate the install prompt with name, icons, theme color, etc.
    """
    return JSONResponse(
        content={
            "name": "Longbox",
            "short_name": "Longbox",
            "description": "Self-hosted comic library manager.",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "orientation": "any",
            "background_color": "#faf6ec",
            "theme_color": "#0a0e1a",
            "icons": [
                # Multi-size PNG entries are what Firefox / older Safari
                # / many Android launchers actually USE for the installed
                # app icon — SVG support in PWA manifests is patchy
                # across browsers. We list 192 + 512 for both purposes
                # plus the SVG as a bonus for engines that DO support
                # it (Chrome / Edge use it for the install prompt).
                {
                    "src": "/icons/icon-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/icons/icon-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any",
                },
                {
                    "src": "/icons/maskable-192.png",
                    "sizes": "192x192",
                    "type": "image/png",
                    "purpose": "maskable",
                },
                {
                    "src": "/icons/maskable-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "maskable",
                },
                {
                    "src": "/static/icons/icon.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any",
                },
            ],
            # Shortcuts surfaced in the OS launcher when long-pressing
            # the installed app icon (Chrome / Android).
            "shortcuts": [
                {"name": "Add a comic", "url": "/add"},
                {"name": "Library",     "url": "/library"},
                {"name": "Stats",       "url": "/stats"},
            ],
            "categories": ["books", "productivity"],
        },
        media_type="application/manifest+json",
    )


_SW_JS = r"""
// Longbox service worker. Network-first for HTML navigations so the
// freshest version always wins when the user is online; cache-first
// for cover images (immutable once downloaded). Mutations and JSON
// APIs are passed straight through.

const CACHE_VERSION = "longbox-v1";

const PRECACHE_URLS = [
  "/",
  "/static/icons/icon.svg",
  "/static/icons/maskable.svg",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/icons/maskable-192.png",
  "/icons/maskable-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) =>
      // Use addAll → fails the whole install if anything 404s; that's
      // a reasonable correctness check. Wrap each in a tolerant catch
      // so a missing icon doesn't break installation.
      Promise.all(
        PRECACHE_URLS.map((u) =>
          fetch(u, { cache: "reload" })
            .then((r) => (r.ok ? cache.put(u, r) : null))
            .catch(() => null),
        ),
      ),
    ),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys.filter((k) => k !== CACHE_VERSION).map((k) => caches.delete(k)),
      ),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;            // mutations pass through

  const url = new URL(req.url);

  // Bypass API + admin endpoints entirely. Caching them would either
  // serve stale JSON or hide destructive POST/DELETE side-effects
  // when a redirect-to-GET runs against a cache.
  if (url.pathname.startsWith("/api/")) return;
  if (url.pathname.startsWith("/admin")) return;

  // Cover images are immutable per URL once they exist locally.
  // Cache-first wins for read latency and works offline.
  if (url.pathname.startsWith("/covers/")) {
    event.respondWith(
      caches.match(req).then((hit) =>
        hit ||
        fetch(req).then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
          }
          return res;
        }),
      ),
    );
    return;
  }

  // HTML navigation: network-first with cache fallback so the page
  // still loads offline (from the last cached copy) but the user
  // always sees the freshest version when online.
  if (req.mode === "navigate" || req.destination === "document") {
    event.respondWith(
      fetch(req)
        .then((res) => {
          if (res.ok) {
            const copy = res.clone();
            caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
          }
          return res;
        })
        .catch(() => caches.match(req).then((hit) => hit || caches.match("/"))),
    );
    return;
  }
});
"""


@router.get("/sw.js")
async def service_worker() -> PlainTextResponse:
    """Service worker JS. Served at the site root so its registration
    scope is `/`. The `Service-Worker-Allowed` header is redundant
    here since the SW URL itself is at root, but kept explicit for
    clarity. `no-cache` on the Cache-Control header makes sure the
    browser checks for updates on every page load — cheap, and the
    refresh story for an SW change is critical."""
    return PlainTextResponse(
        content=_SW_JS,
        media_type="application/javascript",
        headers={
            "Service-Worker-Allowed": "/",
            "Cache-Control": "no-cache",
        },
    )
