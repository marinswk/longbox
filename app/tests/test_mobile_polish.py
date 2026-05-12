"""Phase-1 mobile polish — assert the markup hooks landed in the right
places. Visual / responsive behavior has to be eyeballed in a real
browser, but these tests catch regressions like "the hamburger label
disappeared" or "the safe-area class fell off the bulk bar."
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


# ── 1.1 + 1.7: global mobile CSS in _base.html ─────────────────────────


def test_base_template_includes_viewport_fit_cover():
    """Notched phones need viewport-fit=cover so safe-area-inset applies."""
    with _client() as client:
        r = client.get("/library")
        assert 'viewport-fit=cover' in r.text


def test_base_template_includes_mobile_font_size_rules():
    """iOS Safari zooms in on focus when input font < 16px; the global
    @media rule has to land."""
    with _client() as client:
        r = client.get("/library")
        assert "font-size: 16px" in r.text
        # And the display-font scale-down rule.
        assert ".font-display.text-3xl" in r.text


def test_base_template_exposes_safe_area_utilities():
    with _client() as client:
        r = client.get("/library")
        assert ".pb-safe" in r.text
        assert "env(safe-area-inset-bottom)" in r.text


# ── 1.2: hamburger nav drawer ─────────────────────────────────────────


def test_top_nav_renders_hamburger_button_on_every_page():
    with _client() as client:
        r = client.get("/library")
        # Mobile menu toggle visible + drawer markup present.
        assert 'for="lb-nav-toggle"' in r.text
        assert 'aria-label="Open menu"' in r.text
        assert "lb-nav-drawer" in r.text


def test_nav_drawer_lists_all_top_nav_destinations():
    with _client() as client:
        r = client.get("/library")
        # Each link should appear inside the drawer (will show up twice
        # in the response — once in the desktop nav, once in the drawer).
        for path in ("/library", "/series", "/tags", "/duplicates",
                     "/stats", "/reading-log", "/add", "/admin"):
            assert r.text.count(f'href="{path}"') >= 2


# ── 1.3: filter drawer on /library and /series ────────────────────────


def test_library_page_includes_mobile_filter_drawer_hooks():
    with _client() as client:
        r = client.get("/library")
        assert 'id="lb-filters-fab"' in r.text
        assert 'id="lb-filters-backdrop"' in r.text
        assert 'id="lb-filters-aside"' in r.text
        assert "lb-filters-drawer" in r.text
        # FAB carries the lg:hidden so it disappears on desktop.
        assert "lg:hidden" in r.text


def test_series_page_includes_mobile_filter_drawer_hooks():
    with _client() as client:
        r = client.get("/series")
        assert 'id="lb-filters-fab"' in r.text
        assert 'id="lb-filters-aside"' in r.text


# ── 1.5: safe-area-inset on fixed bottom bars ─────────────────────────


def test_bulk_edit_bar_has_safe_area_padding():
    with _client() as client:
        r = client.get("/library")
        # bulk bar wraps `pb-safe` so on notched phones the home indicator
        # doesn't sit on top of the action buttons.
        assert "pb-safe" in r.text


# ── 1.6: dismissable flash banner ─────────────────────────────────────


def test_admin_flash_banner_has_dismiss_button():
    with _client() as client:
        r = client.get("/admin", params={"flash": "Hello world"})
        assert "Hello world" in r.text
        assert 'aria-label="Dismiss"' in r.text


def test_comic_detail_flash_banner_has_dismiss_button():
    """Re-pick / refresh redirects land with a flash on the comic page."""
    with _client() as client:
        # Need a comic. Add a quick one.
        client.post("/add/save", data={
            "title": "MP Flash", "isbn_13": "9799700000001",
            "series": "MP Flash Series", "publisher": "MP Flash Pub",
        })
        comic_id = next(
            c["id"] for c in client.get("/api/comics", params={"limit": 500}).json()
            if c.get("isbn_13") == "9799700000001"
        )
        r = client.get(f"/comic/{comic_id}", params={"flash": "Refreshed."})
        assert "Refreshed." in r.text
        assert 'aria-label="Dismiss"' in r.text


# ── 1.4: 44px touch targets where it matters ──────────────────────────


# ── Phase 2: layout polish ─────────────────────────────────────────────


def test_comic_detail_compact_sidebar_layout_for_mobile():
    """The cover + sidebar block should be a flex row on mobile (cover
    next to actions) and a flex column on lg+ (the existing sidebar)."""
    with _client() as client:
        client.post("/add/save", data={
            "title": "P2 Detail", "isbn_13": "9799700000201",
            "series": "P2 Detail Series", "publisher": "P2 Pub",
        })
        comic_id = next(
            c["id"] for c in client.get("/api/comics", params={"limit": 500}).json()
            if c.get("isbn_13") == "9799700000201"
        )
        page = client.get(f"/comic/{comic_id}").text
        # Mobile: small cover (w-32). Desktop: full-width inside sidebar (lg:w-full).
        assert "w-32 flex-none" in page
        assert "lg:w-full" in page
        # Secondary actions hide behind a disclosure on mobile.
        assert "lb-mobile-disclosure" in page


def test_copies_table_has_responsive_stack_class():
    """The copies table gets `lb-stack-mobile` so it renders as cards
    on phones via the matching CSS in `_base.html`."""
    with _client() as client:
        client.post("/add/save", data={
            "title": "P2 Copies", "isbn_13": "9799700000301",
            "series": "P2 Copies Series", "publisher": "P2 Pub",
        })
        comic_id = next(
            c["id"] for c in client.get("/api/comics", params={"limit": 500}).json()
            if c.get("isbn_13") == "9799700000301"
        )
        page = client.get(f"/comic/{comic_id}").text
        assert "lb-stack-mobile" in page
        # And the CSS rule exists in the base template.
        assert "table.lb-stack-mobile" in page


def test_base_template_includes_responsive_table_css():
    with _client() as client:
        r = client.get("/library")
        # The stacked-card responsive table styles live in _base.html so
        # every page that uses `lb-stack-mobile` benefits.
        assert "table.lb-stack-mobile" in r.text


def test_admin_sub_nav_pills_have_min_height_44():
    with _client() as client:
        r = client.get("/admin")
        # The sub-nav anchors are wrapped in `min-h-11` (44px) on mobile.
        assert "min-h-11" in r.text
