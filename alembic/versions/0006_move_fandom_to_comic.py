"""move fandom from Series to Comic

Revision ID: 0006
Revises: 0005
Create Date: 2026-05-08

`Series.fandom` was scaffolded in the initial schema but never wired into
the app. Moving it onto Comic so each issue carries its own fandom — that
handles one-shots / orphan comics with no series, and makes the filter a
single-table query.

The upgrade path is best-effort: any pre-existing Series.fandom values are
copied onto every Comic in that series before the column is dropped.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("comic") as batch:
        batch.add_column(sa.Column("fandom", sa.String(), nullable=True))
    op.create_index("ix_comic_fandom", "comic", ["fandom"], unique=False)

    # Best-effort backfill from the (unused) Series.fandom column. Even if
    # nothing was there, this is a safe no-op.
    op.execute(
        """
        UPDATE comic
           SET fandom = (
                SELECT s.fandom FROM series s WHERE s.id = comic.series_id
           )
         WHERE comic.series_id IS NOT NULL
           AND comic.fandom IS NULL
        """
    )

    op.drop_index("ix_series_fandom", table_name="series")
    with op.batch_alter_table("series") as batch:
        batch.drop_column("fandom")


def downgrade() -> None:
    with op.batch_alter_table("series") as batch:
        batch.add_column(sa.Column("fandom", sa.String(), nullable=True))
    op.create_index("ix_series_fandom", "series", ["fandom"], unique=False)

    # Restore by taking any one comic's fandom per series (MAX is arbitrary
    # but deterministic given identical values across siblings).
    op.execute(
        """
        UPDATE series
           SET fandom = (
                SELECT MAX(c.fandom) FROM comic c WHERE c.series_id = series.id
           )
        """
    )

    op.drop_index("ix_comic_fandom", table_name="comic")
    with op.batch_alter_table("comic") as batch:
        batch.drop_column("fandom")
