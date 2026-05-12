"""MetadataCache eviction.

`prune_expired()` deletes rows older than `metadata_cache_ttl_days` (30 by
default). It runs in the FastAPI lifespan so every cold start sweeps the
table.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import MetadataCache
from app.services.cache import prune_expired


def _client() -> TestClient:
    return TestClient(create_app())


def _seed(*rows: tuple[str, str, datetime]) -> None:
    async def _go():
        async with SessionLocal() as session:
            for source, key, when in rows:
                session.add(MetadataCache(
                    source=source, key=key, payload="{}", fetched_at=when,
                ))
            await session.commit()
    asyncio.run(_go())


def _count(source: str) -> int:
    async def _go():
        async with SessionLocal() as session:
            rows = (await session.exec(
                select(MetadataCache).where(MetadataCache.source == source)
            )).all()
            return len(rows)
    return asyncio.run(_go())


def test_prune_drops_rows_older_than_ttl_keeps_fresh():
    with _client():  # ensures the lifespan ran tables migrations
        pass
    now = datetime.now(UTC)
    _seed(
        ("prune-test", "old-1", now - timedelta(days=60)),
        ("prune-test", "old-2", now - timedelta(days=31)),
        ("prune-test", "fresh", now - timedelta(days=1)),
    )
    deleted = asyncio.run(prune_expired())
    # At least the two seeded olds; could be more if other tests left rows.
    assert deleted >= 2
    assert _count("prune-test") == 1


def test_prune_respects_max_age_override():
    with _client():
        pass
    now = datetime.now(UTC)
    _seed(
        ("prune-override", "a", now - timedelta(days=10)),
        ("prune-override", "b", now - timedelta(days=2)),
    )
    # 5-day cutoff drops "a" but keeps "b".
    asyncio.run(prune_expired(max_age_days=5))
    assert _count("prune-override") == 1
