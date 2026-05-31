"""DB-backed application settings + a synchronous runtime credential cache.

Upstream-source credentials (ComicVine API key, Metron username /
password) live in the `Setting` table so they can be managed from the
/admin page without a container restart. But the source clients
(`comicvine.py`, `metron.py`) read their credentials on every call and
must stay synchronous — an async DB hit per upstream request would be
both slow and awkward.

The bridge is a process-global **runtime cache** (`CREDS`) that the
source clients read synchronously. It's hydrated from the DB (falling
back to env) once at startup and again whenever the admin form saves a
key, so reads are always in-memory and a saved key takes effect
immediately.

Precedence: a value stored in the DB wins; if absent, the matching
environment variable is used. This keeps existing env-configured
deployments working untouched, and lets a user move a key into the DB
(then delete the env var) with no flag day.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional

from sqlmodel import select

from app.config import settings
from app.db import SessionLocal
from app.models import Setting

# Setting keys. Kept as constants so the admin router, the hydrate
# routine, and tests all agree on the spelling.
KEY_COMICVINE_API_KEY = "comicvine_api_key"
KEY_METRON_USER = "metron_user"
KEY_METRON_PASS = "metron_pass"

CREDENTIAL_KEYS = (KEY_COMICVINE_API_KEY, KEY_METRON_USER, KEY_METRON_PASS)


@dataclass
class _RuntimeCredentials:
    """In-memory snapshot the source clients read synchronously."""
    comicvine_api_key: Optional[str] = None
    metron_user: Optional[str] = None
    metron_pass: Optional[str] = None


# Process-global. Initialised from env so imports work before the
# lifespan hydrate runs (and so tests that never hit the DB still see
# env values).
CREDS = _RuntimeCredentials(
    comicvine_api_key=settings.comicvine_api_key,
    metron_user=settings.metron_user,
    metron_pass=settings.metron_pass,
)


# ── DB key/value access ────────────────────────────────────────────────


async def get(key: str) -> Optional[str]:
    """Return the stored value for `key`, or None if unset."""
    async with SessionLocal() as session:
        row = await session.get(Setting, key)
        return row.value if row else None


async def set_value(key: str, value: Optional[str]) -> None:
    """Upsert a setting. Empty / None deletes the row so the env
    fallback applies again."""
    async with SessionLocal() as session:
        row = await session.get(Setting, key)
        cleaned = (value or "").strip() or None
        if cleaned is None:
            if row is not None:
                await session.delete(row)
                await session.commit()
            return
        if row is None:
            row = Setting(key=key, value=cleaned, updated_at=datetime.now(UTC))
        else:
            row.value = cleaned
            row.updated_at = datetime.now(UTC)
        session.add(row)
        await session.commit()


async def get_many(keys: tuple[str, ...]) -> dict[str, Optional[str]]:
    async with SessionLocal() as session:
        rows = (await session.exec(
            select(Setting).where(Setting.key.in_(keys))
        )).all()
    by_key = {r.key: r.value for r in rows}
    return {k: by_key.get(k) for k in keys}


# ── Runtime credential cache ───────────────────────────────────────────


async def hydrate_credentials() -> None:
    """Load source credentials from the DB (falling back to env) into
    the process-global `CREDS`. Called at lifespan startup and after
    every admin save so the source clients see fresh values without a
    restart."""
    stored = await get_many(CREDENTIAL_KEYS)
    CREDS.comicvine_api_key = stored[KEY_COMICVINE_API_KEY] or settings.comicvine_api_key
    CREDS.metron_user = stored[KEY_METRON_USER] or settings.metron_user
    CREDS.metron_pass = stored[KEY_METRON_PASS] or settings.metron_pass


async def save_credentials(
    *,
    comicvine_api_key: Optional[str] = None,
    metron_user: Optional[str] = None,
    metron_pass: Optional[str] = None,
    update_comicvine: bool = True,
    update_metron_user: bool = True,
    update_metron_pass: bool = True,
) -> None:
    """Persist whichever credential fields the admin form submitted,
    then re-hydrate the runtime cache.

    The `update_*` flags let the form leave a field blank to mean
    "keep the current value" (so the secret never has to be re-typed
    or re-rendered). Pass the flag False to skip writing that key."""
    if update_comicvine:
        await set_value(KEY_COMICVINE_API_KEY, comicvine_api_key)
    if update_metron_user:
        await set_value(KEY_METRON_USER, metron_user)
    if update_metron_pass:
        await set_value(KEY_METRON_PASS, metron_pass)
    await hydrate_credentials()


# ── Status helpers for the admin UI ────────────────────────────────────


def _mask(value: Optional[str]) -> Optional[str]:
    """Show only the last 4 chars of a secret, e.g. '••••1845'.
    Returns None when unset."""
    if not value:
        return None
    tail = value[-4:]
    return "••••" + tail


async def credentials_status() -> dict[str, dict]:
    """For the admin page: per-credential {configured, masked, source}
    where `source` is 'db' / 'env' / None. Never returns the raw
    secret."""
    stored = await get_many(CREDENTIAL_KEYS)

    def _status(db_val: Optional[str], env_val: Optional[str]) -> dict:
        effective = db_val or env_val
        return {
            "configured": bool(effective),
            "masked": _mask(effective),
            "source": "db" if db_val else ("env" if env_val else None),
        }

    return {
        "comicvine": _status(stored[KEY_COMICVINE_API_KEY], settings.comicvine_api_key),
        "metron_user": _status(stored[KEY_METRON_USER], settings.metron_user),
        "metron_pass": _status(stored[KEY_METRON_PASS], settings.metron_pass),
    }
