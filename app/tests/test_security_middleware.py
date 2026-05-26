"""Origin-check middleware + TrustedHost wiring tests.

Both layers are opt-in via env. The defaults preserve the LAN-only
single-user experience (no friction); the tests here verify that
flipping the env knobs actually enables the protection.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.middleware import OriginCheckMiddleware


def test_origin_check_parses_comma_separated_setting():
    assert OriginCheckMiddleware.parse_setting("") == []
    assert OriginCheckMiddleware.parse_setting("  ") == []
    assert OriginCheckMiddleware.parse_setting(
        "http://a.example, https://b.example"
    ) == ["http://a.example", "https://b.example"]
    # Trailing comma / blank tokens are dropped, not preserved as ''.
    assert OriginCheckMiddleware.parse_setting("http://a.example,,") == [
        "http://a.example"
    ]


def test_origin_check_no_op_when_unconfigured(monkeypatch):
    """Default empty allowlist → middleware is wired only when
    `csrf_allowed_origins` is set, so an unconfigured app accepts
    every POST regardless of Origin (matches first-run behaviour)."""
    monkeypatch.setattr("app.main.settings.csrf_allowed_origins", "")
    with TestClient(create_app()) as client:
        # `/add/lookup` is a POST handler; with no auth and no origin
        # check, a cross-origin request just works.
        r = client.post(
            "/add/lookup",
            data={"identifier": "9999999999999"},
            headers={"Origin": "http://evil.example"},
        )
        assert r.status_code in (200, 422)  # depends on whether lookup found anything; not 403


def test_origin_check_allows_matching_origin(monkeypatch):
    """When `csrf_allowed_origins` IS set, a same-origin POST goes
    through normally."""
    monkeypatch.setattr(
        "app.main.settings.csrf_allowed_origins",
        "http://longbox.lan:8080,http://localhost:8000",
    )
    with TestClient(create_app()) as client:
        r = client.post(
            "/add/lookup",
            data={"identifier": "9999999999998"},
            headers={"Origin": "http://longbox.lan:8080"},
        )
        assert r.status_code != 403


def test_origin_check_rejects_mismatched_origin(monkeypatch):
    """A cross-origin POST whose Origin isn't on the list gets 403."""
    monkeypatch.setattr(
        "app.main.settings.csrf_allowed_origins",
        "http://longbox.lan:8080",
    )
    with TestClient(create_app()) as client:
        r = client.post(
            "/add/lookup",
            data={"identifier": "9999999999997"},
            headers={"Origin": "http://evil.example"},
        )
        assert r.status_code == 403
        assert "evil.example" in r.text


def test_origin_check_allows_missing_origin(monkeypatch):
    """No Origin header at all = not a browser cross-origin POST.
    Curl, Home Assistant, anything scripted — keep working."""
    monkeypatch.setattr(
        "app.main.settings.csrf_allowed_origins",
        "http://longbox.lan:8080",
    )
    with TestClient(create_app()) as client:
        r = client.post("/add/lookup", data={"identifier": "9999999999996"})
        assert r.status_code != 403


def test_origin_check_passes_get_requests(monkeypatch):
    """GET / HEAD never trigger the check (no destructive action
    possible). A malicious cross-origin GET to /admin shouldn't 403."""
    monkeypatch.setattr(
        "app.main.settings.csrf_allowed_origins",
        "http://longbox.lan:8080",
    )
    with TestClient(create_app()) as client:
        r = client.get("/admin", headers={"Origin": "http://evil.example"})
        assert r.status_code == 200


def test_origin_check_blocks_destructive_wipe(monkeypatch):
    """The whole reason this middleware exists: a cross-origin POST
    to /admin/wipe gets stopped before it can fire."""
    monkeypatch.setattr(
        "app.main.settings.csrf_allowed_origins",
        "http://longbox.lan:8080",
    )
    with TestClient(create_app()) as client:
        r = client.post(
            "/admin/wipe",
            data={"confirm": "WIPE EVERYTHING"},
            headers={"Origin": "http://evil.example"},
        )
        assert r.status_code == 403


def test_trusted_host_blocks_unconfigured_host(monkeypatch):
    """TrustedHostMiddleware is wired only when ALLOWED_HOSTS is
    tighter than the `*` default. When tightened, a request with a
    spoofed Host header gets 400."""
    monkeypatch.setattr("app.main.settings.allowed_hosts", "longbox.lan,localhost")
    with TestClient(create_app(), base_url="http://longbox.lan") as client:
        # Default same-host request still works.
        r = client.get("/health")
        assert r.status_code == 200
    # A request with a Host header outside the allowlist is rejected.
    with TestClient(create_app(), base_url="http://evil.example") as client:
        r = client.get("/health")
        assert r.status_code == 400
