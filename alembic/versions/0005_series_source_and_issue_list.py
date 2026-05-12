"""add Series.source / source_id / expected_issues for missing-issues detection

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-29

Lets each Series carry the provenance fields needed to refresh its issue
list from the source it was originally added from, plus the cached list
of expected issue article titles itself.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("series") as batch:
        batch.add_column(sa.Column("source", sa.String(), nullable=True))
        batch.add_column(sa.Column("source_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("expected_issues", sa.String(), nullable=True))
    op.create_index("ix_series_source", "series", ["source"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_series_source", table_name="series")
    with op.batch_alter_table("series") as batch:
        batch.drop_column("expected_issues")
        batch.drop_column("source_id")
        batch.drop_column("source")
