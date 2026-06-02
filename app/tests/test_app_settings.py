"""DB-backed source credentials + the /admin API-key management UI."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import app_settings
from app.services.app_settings import (
    KEY_COMICVINE_API_KEY, KEY_GOOGLE_BOOKS_API_KEY,
    KEY_METRON_PASS, KEY_METRON_USER,
)


def _client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def _reset_credentials():
    """Clear any DB-stored credentials and re-hydrate the runtime cache
    after each test, so a key stored by one test can't leak into another
    (the `setting` table + the `CREDS` global both persist across the
    shared test session). After reset, CREDS reflects the conftest env
    defaults — the same baseline tests saw before this feature existed."""
    yield
    with _client():
        for k in (KEY_COMICVINE_API_KEY, KEY_METRON_USER, KEY_METRON_PASS,
                  KEY_GOOGLE_BOOKS_API_KEY):
            asyncio.run(app_settings.set_value(k, ""))
        asyncio.run(app_settings.hydrate_credentials())


def test_set_get_and_delete_roundtrip():
    with _client():  # ensures the `setting` table exists
        async def _go():
            await app_settings.set_value(KEY_COMICVINE_API_KEY, "cv-key-123")
            assert await app_settings.get(KEY_COMICVINE_API_KEY) == "cv-key-123"
            # Empty value deletes the row (env fallback re-applies).
            await app_settings.set_value(KEY_COMICVINE_API_KEY, "")
            assert await app_settings.get(KEY_COMICVINE_API_KEY) is None
        asyncio.run(_go())


def test_hydrate_prefers_db_over_env(monkeypatch):
    """A DB value wins over the env var; absence falls back to env."""
    monkeypatch.setattr(app_settings.settings, "comicvine_api_key", "env-cv")
    with _client():
        async def _go():
            # No DB row yet → env value hydrates.
            await app_settings.set_value(KEY_COMICVINE_API_KEY, "")
            await app_settings.hydrate_credentials()
            assert app_settings.CREDS.comicvine_api_key == "env-cv"
            # DB row set → DB value wins.
            await app_settings.set_value(KEY_COMICVINE_API_KEY, "db-cv")
            await app_settings.hydrate_credentials()
            assert app_settings.CREDS.comicvine_api_key == "db-cv"
        asyncio.run(_go())
    # cleanup so the global cache doesn't leak into other tests
    asyncio.run(app_settings.set_value(KEY_COMICVINE_API_KEY, ""))


def test_saving_comicvine_key_makes_source_configured(monkeypatch):
    """End-to-end: saving a key via the admin POST flips
    comicvine.is_configured() to True without a restart. (conftest sets
    a CV env key, so we null the env fallback to get a clean
    unconfigured baseline first.)"""
    from app.services import comicvine
    monkeypatch.setattr(app_settings.settings, "comicvine_api_key", None)
    with _client() as client:
        asyncio.run(app_settings.set_value(KEY_COMICVINE_API_KEY, ""))
        asyncio.run(app_settings.hydrate_credentials())
        assert comicvine.is_configured() is False

        r = client.post("/admin/api-keys", data={"comicvine_api_key": "fresh-key"})
        assert r.status_code == 200
        assert "Saved" in r.text
        # Runtime cache updated immediately.
        assert comicvine.is_configured() is True


def test_admin_status_masks_secret_and_never_renders_raw():
    with _client() as client:
        asyncio.run(app_settings.set_value(KEY_COMICVINE_API_KEY, "supersecret9999"))
        asyncio.run(app_settings.hydrate_credentials())
        page = client.get("/admin").text
        # The Sources section renders, masked.
        assert "SOURCES" in page
        assert "••••9999" in page
        # The raw secret must never appear in the HTML.
        assert "supersecret9999" not in page
    asyncio.run(app_settings.set_value(KEY_COMICVINE_API_KEY, ""))
    asyncio.run(app_settings.hydrate_credentials())


def test_blank_field_keeps_existing_value():
    """Submitting the form with a blank ComicVine field keeps the
    stored key (so the secret never has to be re-typed)."""
    with _client() as client:
        asyncio.run(app_settings.set_value(KEY_COMICVINE_API_KEY, "keep-me"))
        asyncio.run(app_settings.hydrate_credentials())
        # Save with blank CV field but a Metron user.
        client.post("/admin/api-keys", data={
            "comicvine_api_key": "", "metron_user": "alice", "metron_pass": "pw",
        })
        assert asyncio.run(app_settings.get(KEY_COMICVINE_API_KEY)) == "keep-me"
        assert asyncio.run(app_settings.get(KEY_METRON_USER)) == "alice"
        assert asyncio.run(app_settings.get(KEY_METRON_PASS)) == "pw"
    for k in (KEY_COMICVINE_API_KEY, KEY_METRON_USER, KEY_METRON_PASS):
        asyncio.run(app_settings.set_value(k, ""))
    asyncio.run(app_settings.hydrate_credentials())


def test_clear_checkbox_removes_stored_key():
    with _client() as client:
        asyncio.run(app_settings.set_value(KEY_COMICVINE_API_KEY, "deleteme"))
        client.post("/admin/api-keys", data={"clear_comicvine": "on"})
        assert asyncio.run(app_settings.get(KEY_COMICVINE_API_KEY)) is None
    asyncio.run(app_settings.hydrate_credentials())


def test_settings_survive_factory_wipe_and_stay_out_of_export():
    """Credentials are config, not library data: a factory wipe must
    NOT clear them, and a backup/export must NOT contain them (so a
    shared backup zip can't leak live keys)."""
    import json
    with _client() as client:
        asyncio.run(app_settings.set_value(KEY_COMICVINE_API_KEY, "survives-wipe"))

        # Export JSON must not carry the setting / its value.
        export = client.get("/api/export").json()
        assert "settings" not in export
        assert "survives-wipe" not in json.dumps(export)

        # Factory wipe leaves the credential intact.
        r = client.post("/admin/wipe", data={"confirm": "WIPE EVERYTHING"})
        assert r.status_code == 200
        assert asyncio.run(app_settings.get(KEY_COMICVINE_API_KEY)) == "survives-wipe"

    asyncio.run(app_settings.set_value(KEY_COMICVINE_API_KEY, ""))
    asyncio.run(app_settings.hydrate_credentials())


def test_google_books_key_managed_via_admin(monkeypatch):
    """The Google Books key follows the same DB-backed admin flow:
    saving flips googlebooks.is_configured() on, masked in the status."""
    from app.services import googlebooks
    from app.services.app_settings import KEY_GOOGLE_BOOKS_API_KEY
    monkeypatch.setattr(app_settings.settings, "google_books_api_key", None)
    with _client() as client:
        asyncio.run(app_settings.set_value(KEY_GOOGLE_BOOKS_API_KEY, ""))
        asyncio.run(app_settings.hydrate_credentials())
        assert googlebooks.is_configured() is False

        r = client.post("/admin/api-keys", data={"google_books_api_key": "gb-key-7777"})
        assert r.status_code == 200
        assert googlebooks.is_configured() is True
        # Status section masks it.
        assert "••••7777" in client.get("/admin").text
        assert "gb-key-7777" not in client.get("/admin").text
