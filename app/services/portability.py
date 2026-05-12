"""Export / import the entire library as a single JSON document.

Goals:

* **Full-fidelity round-trip** — every user-authored row (comics, copies,
  creators, arcs, tags, wishlist, pull list, plus all join rows) survives
  a wipe + restore.
* **Stable schema** — each export carries a `version` integer so future
  imports can migrate older payloads forward.
* **Atomic import** — the whole load happens inside one transaction; if
  anything fails the DB is left untouched.

What's intentionally out of scope:

* `MetadataCache` is **not** exported — it's just a 30-day API cache and
  re-populates itself from upstream on demand.
* Cover image *files* under `/data/covers` aren't bundled. The remote URL
  is exported, and the post-import flow will re-download covers as the
  detail page requests them. A separate "backup zip" endpoint can pack
  cover files later if needed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Optional

from sqlalchemy import delete
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models import (
    Character,
    Comic,
    ComicArc,
    ComicCharacter,
    ComicCreator,
    ComicTag,
    Copy,
    Creator,
    Publisher,
    Series,
    StoryArc,
    Tag,
)

# Bumped to 2 when Wishlist + PullList tables were dropped, then to 3
# when fandom moved from Series → Comic. v2 backups still import — we
# silently drop any (unused) `Series.fandom` field if present.
EXPORT_VERSION = 3
_ACCEPTED_IMPORT_VERSIONS = {2, 3}

# Order matters for both export (cosmetic) and import (FK satisfaction).
# Parents come before children: Publisher -> Series -> Comic -> Copy/joins.
_ENTITIES_IN_ORDER: list[tuple[str, type]] = [
    ("publishers", Publisher),
    ("series", Series),
    ("creators", Creator),
    ("characters", Character),
    ("story_arcs", StoryArc),
    ("tags", Tag),
    ("comics", Comic),
    ("copies", Copy),
    ("comic_creators", ComicCreator),
    ("comic_characters", ComicCharacter),
    ("comic_arcs", ComicArc),
    ("comic_tags", ComicTag),
]

# Reverse order for delete-all so FKs come down cleanly.
_DELETE_ORDER = list(reversed(_ENTITIES_IN_ORDER))


def _to_jsonable(value: Any) -> Any:
    """JSON-encode date/datetime as ISO-8601 strings; leave everything else."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return value


def _row_to_dict(row) -> dict[str, Any]:
    return {k: _to_jsonable(v) for k, v in row.model_dump().items()}


_DATE_FIELDS = {"cover_date", "purchase_date", "date_read", "lent_on"}
_DATETIME_FIELDS = {"created_at", "updated_at", "added_at", "started_at", "fetched_at"}


def _coerce(field: str, value: Any) -> Any:
    """Reverse of `_to_jsonable` for a known field name."""
    if value is None:
        return None
    if field in _DATE_FIELDS and isinstance(value, str):
        return date.fromisoformat(value)
    if field in _DATETIME_FIELDS and isinstance(value, str):
        # Python's fromisoformat handles offset suffixes from 3.11+.
        return datetime.fromisoformat(value)
    return value


async def export_all(session: AsyncSession) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": EXPORT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
    }
    for key, model in _ENTITIES_IN_ORDER:
        result = await session.exec(select(model))
        rows = list(result.all())
        payload[key] = [_row_to_dict(r) for r in rows]
    return payload


async def import_all(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    wipe_existing: bool = True,
) -> dict[str, int]:
    """Replace the library with the contents of `payload`.

    With `wipe_existing=True` (the default), every user-authored table is
    truncated first. The full insert + the wipe happen in a single
    transaction, so an exception leaves the original data intact.

    Returns a `{entity_name: row_count}` summary of what was inserted.
    """
    version = payload.get("version")
    if version not in _ACCEPTED_IMPORT_VERSIONS:
        raise ValueError(
            f"unsupported export version {version!r} (expected one of {sorted(_ACCEPTED_IMPORT_VERSIONS)})"
        )

    # v2 backups still carry an unused `Series.fandom` field that the v3
    # schema dropped. Silently strip it so import doesn't choke.
    if version == 2:
        for series_row in payload.get("series") or []:
            series_row.pop("fandom", None)

    inserted: dict[str, int] = {}
    if wipe_existing:
        for _key, model in _DELETE_ORDER:
            await session.exec(delete(model))

    for key, model in _ENTITIES_IN_ORDER:
        rows = payload.get(key) or []
        for raw in rows:
            coerced = {k: _coerce(k, v) for k, v in raw.items()}
            session.add(model(**coerced))
        inserted[key] = len(rows)
        # Flush per-entity so child FKs see freshly-inserted parents.
        await session.flush()

    await session.commit()
    return inserted
