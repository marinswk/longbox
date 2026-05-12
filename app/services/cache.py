"""Read-through cache over the MetadataCache table.

Each call opens its own short-lived AsyncSession so concurrent fan-out
calls (ComicVine + Open Library in parallel) don't share a session.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlmodel import select

from app.config import settings
from app.db import SessionLocal
from app.models import MetadataCache


def _now() -> datetime:
    return datetime.now(UTC)


async def prune_expired(*, max_age_days: int | None = None) -> int:
    """Delete MetadataCache rows older than `max_age_days` (defaults to
    `settings.metadata_cache_ttl_days`). Returns the number of rows deleted.

    Cheap to call — single DELETE on an indexed timestamp column. Wired
    into the FastAPI lifespan so every cold start sweeps stale rows; the
    next process restart re-runs the sweep.
    """
    ttl = timedelta(days=max_age_days if max_age_days is not None else settings.metadata_cache_ttl_days)
    cutoff = _now() - ttl
    async with SessionLocal() as session:
        result = await session.exec(
            sa_delete(MetadataCache).where(MetadataCache.fetched_at < cutoff)
        )
        await session.commit()
        return int(result.rowcount or 0)


async def get_or_set(
    *,
    source: str,
    key: str,
    fetch: Callable[[], Awaitable[Any]],
    ttl_days: int | None = None,
) -> Any:
    ttl = timedelta(days=ttl_days if ttl_days is not None else settings.metadata_cache_ttl_days)

    async with SessionLocal() as session:
        result = await session.exec(
            select(MetadataCache).where(MetadataCache.source == source, MetadataCache.key == key)
        )
        row = result.first()
        if row is not None:
            fetched_at = row.fetched_at if row.fetched_at.tzinfo else row.fetched_at.replace(tzinfo=UTC)
            if _now() - fetched_at <= ttl:
                return json.loads(row.payload)

    payload = await fetch()

    async with SessionLocal() as session:
        result = await session.exec(
            select(MetadataCache).where(MetadataCache.source == source, MetadataCache.key == key)
        )
        row = result.first()
        if row is None:
            row = MetadataCache(source=source, key=key, payload=json.dumps(payload), fetched_at=_now())
        else:
            row.payload = json.dumps(payload)
            row.fetched_at = _now()
        session.add(row)
        await session.commit()
    return payload
