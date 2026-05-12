"""drop wishlist + pulllist tables (features dropped from scope)

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-29

The wishlist and pull-list features were removed before any UI was
built. The empty tables are dropped here. Any rows that may exist on
older deployments are lost.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_pulllist_series_id", table_name="pulllist")
    op.drop_table("pulllist")
    op.drop_index("ix_wishlist_comic_id", table_name="wishlist")
    op.drop_table("wishlist")


def downgrade() -> None:
    op.create_table(
        "wishlist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("comic_id", sa.Integer(), nullable=True),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("added_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["comic_id"], ["comic.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_wishlist_comic_id", "wishlist", ["comic_id"], unique=False)
    op.create_table(
        "pulllist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_pulllist_series_id", "pulllist", ["series_id"], unique=False)
