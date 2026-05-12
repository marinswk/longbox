"""PWA endpoints — web manifest + service worker.

Both are served dynamically rather than as static files so the values
stay in sync with whatever the app is currently themed to.

  GET /manifest.webmanifest   — PWA install metadata
  GET /sw.js                  — service worker JS (root-scoped)

The service worker uses a network-first strategy for HTML navigation
(so a fresh deploy is picked up on the next page load) and a
cache-first strategy for cover images under `/covers/` (immutable
once downloaded — perfect cache candidates). POST/PUT/DELETE and
`/api/*` requests are passed through untouched.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse, PlainTextResponse

router = APIRouter(tags=["pwa"])


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
                # A single SVG covers every browser that supports it (every
                # modern engine + iOS 13+). `purpose: any` is the default;
                # the maskable variant is a separate entry below.
                {
                    "src": "/static/icons/icon.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "any",
                },
                {
                    "src": "/static/icons/maskable.svg",
                    "sizes": "any",
                    "type": "image/svg+xml",
                    "purpose": "maskable",
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
