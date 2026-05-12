"""Mobile-polish phase 4 — fullscreen barcode scanner on /add.

We can't test camera behaviour in unit-land (no browser, no
permissions, no MediaStream). These tests assert the markup hooks
are present so the JS state machine has a stable shape to bind to.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_add_page_includes_fullscreen_scanner_overlay():
    with _client() as client:
        r = client.get("/add")
        assert r.status_code == 200
        # Fullscreen overlay container exists, role=dialog for screen readers.
        assert 'id="scanner-overlay"' in r.text
        assert 'role="dialog"' in r.text
        assert 'aria-modal="true"' in r.text


def test_scanner_has_corner_brackets_for_aiming():
    with _client() as client:
        r = client.get("/add")
        # Four corner-bracket spans for visual aim guides.
        assert r.text.count('border-crawl') >= 4


def test_scanner_exposes_torch_button_initially_hidden():
    with _client() as client:
        r = client.get("/add")
        assert 'id="scanner-torch"' in r.text
        # Hidden until the JS detects torch capability on the active track.
        assert "hidden" in r.text


def test_scanner_close_button_present():
    with _client() as client:
        r = client.get("/add")
        assert 'id="scanner-close"' in r.text
        assert "✕ Close" in r.text


def test_scanner_js_state_machine_hooks_wired():
    with _client() as client:
        body = client.get("/add").text
        # Start/stop/torch flow plus haptic + escape close.
        assert "navigator.vibrate" in body
        assert "applyConstraints" in body          # torch
        assert "getCapabilities" in body           # torch feature detection
        assert "facingMode: 'environment'" in body # rear-camera preference
        assert 'key === \'Escape\'' in body or "key === 'Escape'" in body


def test_scanner_loads_html5_qrcode_library():
    with _client() as client:
        r = client.get("/add")
        assert "html5-qrcode" in r.text
