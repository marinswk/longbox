"""App version surface — /health JSON + the /admin page badge."""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.main import create_app
from app.version import __version__


def _client() -> TestClient:
    return TestClient(create_app())


def test_version_string_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__


def test_health_endpoint_reports_version():
    with _client() as client:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["version"] == __version__


def test_admin_page_shows_version_badge():
    with _client() as client:
        html = client.get("/admin").text
        assert f"Longbox v{__version__}" in html
