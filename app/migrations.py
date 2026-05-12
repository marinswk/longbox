"""Alembic runner used by the FastAPI lifespan.

Three startup scenarios:

1. Fresh DB — no tables at all. Run `alembic upgrade head`, which creates
   the schema from the baseline migration onwards.
2. Existing DB created by `SQLModel.metadata.create_all` (pre-Alembic
   cutover) — tables are present but `alembic_version` isn't. Stamp `head`
   so subsequent migrations apply, but don't try to re-create the schema.
3. DB already managed by Alembic — `alembic_version` exists. Just run
   `upgrade head` so any new revisions are applied.

The Alembic env.py uses a synchronous engine, so we call into it via
`asyncio.to_thread` to avoid blocking the FastAPI event loop.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.config import settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def _alembic_config() -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return cfg


def _classify_db() -> str:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    sync_engine = create_engine(f"sqlite:///{settings.data_dir / 'longbox.db'}")
    try:
        with sync_engine.connect() as conn:
            insp = inspect(conn)
            names = set(insp.get_table_names())
    finally:
        sync_engine.dispose()
    if not names:
        return "fresh"
    if "alembic_version" in names:
        return "managed"
    return "legacy"


def _run_sync() -> None:
    cfg = _alembic_config()
    state = _classify_db()
    if state == "legacy":
        # Pre-existing schema from create_all — adopt without rewriting.
        command.stamp(cfg, "head")
    else:
        # Fresh or already-managed: bring schema up to head.
        command.upgrade(cfg, "head")


async def run_migrations() -> None:
    await asyncio.to_thread(_run_sync)
