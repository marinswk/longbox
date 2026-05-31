"""Setting key/value table for DB-backed app config

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-31

Backs the upstream-source credentials (ComicVine API key, Metron
username/password) so they can be managed from the /admin page rather
than only via environment variables. Plain key/value strings; the
table is deliberately small and generic so future settings can reuse
it without another migration.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "setting",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.String(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("setting")
