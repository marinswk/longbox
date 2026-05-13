"""PWA endpoints — manifest, service worker, SVG icons, base-template hooks."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


# ── 3.1 SVG icons ──────────────────────────────────────────────────────


def test_default_icon_svg_is_served():
    with _client() as client:
        r = client.get("/static/icons/icon.svg")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/svg+xml")
        # SVG carries the brand colors so any platform that uses it as
        # a favicon still gets the crawl-yellow look.
        assert "#FFE81F" in r.text


def test_maskable_icon_svg_is_served():
    with _client() as client:
        r = client.get("/static/icons/maskable.svg")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/svg+xml")


# ── 3.2 Manifest ───────────────────────────────────────────────────────


def test_manifest_returns_valid_json_with_required_fields():
    with _client() as client:
        r = client.get("/manifest.webmanifest")
        assert r.status_code == 200
        assert "manifest+json" in r.headers["content-type"]
        data = json.loads(r.text)
        # Required fields for Chrome's install prompt eligibility.
        assert data["name"] == "Longbox"
        assert data["start_url"] == "/"
        assert data["display"] in ("standalone", "fullscreen", "minimal-ui")
        assert data["icons"]
        # Must include at least one icon with size >= 192 (or "any"),
        # and at least one maskable icon for adaptive Android shapes.
        sizes = [i.get("sizes") for i in data["icons"]]
        assert any(s == "any" or "512" in s for s in sizes)
        purposes = [i.get("purpose") for i in data["icons"]]
        assert "maskable" in purposes


def test_manifest_lists_png_icons_for_firefox_and_legacy_launchers():
    """Firefox on Android (and several other PWA install paths) won't
    accept SVG manifest icons — they read PNGs at specific pixel
    sizes. The manifest must list 192x192 + 512x512 PNGs for both
    'any' and 'maskable' purposes so the install actually shows our
    icon instead of a blank placeholder."""
    with _client() as client:
        r = client.get("/manifest.webmanifest")
        data = json.loads(r.text)
        png_icons = [i for i in data["icons"] if i.get("type") == "image/png"]
        keys = {(i["sizes"], i["purpose"]) for i in png_icons}
        assert ("192x192", "any") in keys
        assert ("512x512", "any") in keys
        assert ("192x192", "maskable") in keys
        assert ("512x512", "maskable") in keys


def test_icon_png_route_renders_actual_png_bytes():
    """/icons/icon-{size}.png and /icons/maskable-{size}.png render
    PNGs from Python (no precomputed binaries). Verify they return
    the PNG magic header and a non-trivial body."""
    with _client() as client:
        for path in (
            "/icons/icon-192.png", "/icons/icon-512.png",
            "/icons/maskable-192.png", "/icons/maskable-512.png",
            "/icons/icon-180.png",
        ):
            r = client.get(path)
            assert r.status_code == 200, f"{path} → {r.status_code}"
            assert r.headers["content-type"] == "image/png"
            assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
            # Real PNG, not an empty stub. Solid-fill maskable variants
            # compress aggressively (~400 bytes at 192px), so the
            # threshold has to allow for that.
            assert len(r.content) > 300


def test_icon_png_route_rejects_unsupported_sizes():
    """Only the sizes referenced by the manifest are valid; arbitrary
    integers from poking around return 404. Keeps the cache from
    growing unbounded under hostile traffic."""
    with _client() as client:
        r = client.get("/icons/icon-9999.png")
        assert r.status_code == 404


def test_icon_png_route_sets_long_cache_headers():
    """Installed-app icons never change for the life of a deployment.
    A short cache would re-render every install / app launch."""
    with _client() as client:
        r = client.get("/icons/icon-192.png")
        cache = r.headers.get("cache-control", "")
        assert "max-age" in cache
        assert "immutable" in cache


def test_base_template_links_png_apple_touch_icon():
    """Apple's apple-touch-icon meta only accepts PNG — SVG silently
    falls back to a screenshot of the first-load page on iOS."""
    with _client() as client:
        r = client.get("/library")
        assert 'rel="apple-touch-icon"' in r.text
        assert "/icons/icon-180.png" in r.text


# ── 3.3 Service worker ────────────────────────────────────────────────


def test_service_worker_is_served_as_javascript():
    with _client() as client:
        r = client.get("/sw.js")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/javascript")
        assert "no-cache" in r.headers.get("cache-control", "").lower()
        assert r.headers.get("service-worker-allowed") == "/"


def test_service_worker_implements_install_activate_fetch():
    with _client() as client:
        body = client.get("/sw.js").text
        assert "addEventListener(\"install\"" in body
        assert "addEventListener(\"activate\"" in body
        assert "addEventListener(\"fetch\"" in body
        # API and admin endpoints bypass the cache.
        assert "/api/" in body
        assert "/admin" in body
        # Covers are cache-first.
        assert "/covers/" in body


# ── 3.4 _base.html hooks ──────────────────────────────────────────────


def test_base_template_links_manifest_and_apple_touch_icon():
    with _client() as client:
        r = client.get("/library")
        assert 'rel="manifest"' in r.text
        assert 'href="/manifest.webmanifest"' in r.text
        assert 'rel="apple-touch-icon"' in r.text
        assert 'apple-mobile-web-app-capable' in r.text
        assert 'name="theme-color"' in r.text


def test_base_template_registers_service_worker():
    with _client() as client:
        r = client.get("/library")
        # SW registration is in the inline script at the bottom of body.
        assert 'navigator.serviceWorker.register("/sw.js")' in r.text


def test_base_template_captures_beforeinstallprompt():
    with _client() as client:
        r = client.get("/library")
        assert "beforeinstallprompt" in r.text
        assert "window.lbInstall" in r.text


# ── 3.5 Install button on home page ──────────────────────────────────


def test_home_page_includes_install_button_visible_by_default():
    """Always-visible install button (the older `hidden` default broke
    Firefox + iOS Safari, neither of which fires beforeinstallprompt to
    flip it visible)."""
    with _client() as client:
        r = client.get("/")
        assert "Install Longbox" in r.text
        assert "data-lb-install" in r.text


def test_install_help_modal_is_rendered_on_every_page():
    """The fallback help modal lives in _base.html so any page's install
    button can open it — not just the home page hero."""
    with _client() as client:
        r = client.get("/library")
        assert 'id="lb-install-help"' in r.text
        assert 'id="lb-install-help-body"' in r.text
        assert "lbInstallShowHelp" in r.text


def test_install_help_has_per_browser_steps():
    """Verify the JS carries instructions for the three browsers that
    won't trigger the native prompt path."""
    with _client() as client:
        body = client.get("/").text
        assert "ios-safari" in body
        assert "firefox" in body
        assert "chromium" in body
        # Concrete strings from each set of steps.
        assert "Add to Home Screen" in body         # iOS Safari
        assert "⋮ menu" in body                     # Firefox / Chromium


def test_install_button_falls_back_to_help_modal_when_no_native_prompt():
    """When no `beforeinstallprompt` was captured, the show() function
    must NOT silently no-op — it should open the help modal so users
    on Firefox / iOS Safari get instructions instead of dead clicks."""
    with _client() as client:
        body = client.get("/").text
        # The JS path: `if (p) { p.prompt() } else { lbInstallShowHelp() }`
        assert "lbInstallShowHelp" in body
        assert "p.prompt()" in body


def test_install_button_hides_once_app_is_already_installed():
    with _client() as client:
        body = client.get("/").text
        # Standards-compliant + legacy iOS detection.
        assert "display-mode: standalone" in body
        assert "navigator.standalone" in body
