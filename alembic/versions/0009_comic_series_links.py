"""ComicSeries link table — many-to-many for multi-series membership

Revision ID: 0009
Revises: 0008
Create Date: 2026-05-15

A Comic can belong to more than one Series. Examples:
  - An omnibus collecting issues from KotOR singles AND KotOR: War
    needs to live in BOTH series so /series/{id} reflects coverage.
  - A reprint TPB belonging to Epic Collection AND the original
    singles series.

`Comic.series_id` is retained as a "primary" / denormalized series
pointer for backward compatibility with existing queries (library
grid, stats, etc.). The link table is the source of truth for
membership-related views (/series/{id}, the comic-detail Series
section, missing-issues detection).

Backfill: every existing Comic with a non-null `series_id` gets a
matching row in `comicseries`. The lifespan task
`backfill_comic_series_links` re-runs on every cold start so newly-
added comics from before the table existed catch up too (idempotent).
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "comicseries",
        sa.Column("comic_id", sa.Integer(), sa.ForeignKey("comic.id"),
                  nullable=False, primary_key=True),
        sa.Column("series_id", sa.Integer(), sa.ForeignKey("series.id"),
                  nullable=False, primary_key=True),
        sa.Column("is_primary", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_comicseries_comic", "comicseries", ["comic_id"],
    )
    op.create_index(
        "ix_comicseries_series", "comicseries", ["series_id"],
    )

    # Backfill: one row per existing `Comic.series_id` value, flagged
    # as the primary link. We do this in raw SQL to avoid pulling the
    # full ORM machinery into a migration.
    bind = op.get_bind()
    bind.execute(sa.text(
        "INSERT INTO comicseries (comic_id, series_id, is_primary, created_at) "
        "SELECT id, series_id, 1, CURRENT_TIMESTAMP "
        "FROM comic WHERE series_id IS NOT NULL"
    ))


def downgrade() -> None:
    op.drop_index("ix_comicseries_series", table_name="comicseries")
    op.drop_index("ix_comicseries_comic", table_name="comicseries")
    op.drop_table("comicseries")
