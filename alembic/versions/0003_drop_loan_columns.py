"""drop Copy.lent_to and Copy.lent_on (loan tracking dropped from scope)

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-29

The loan-tracking dashboard was deferred indefinitely; the columns
that supported it on `copy` are dropped here. Existing values are lost.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("copy") as batch:
        batch.drop_column("lent_on")
        batch.drop_column("lent_to")


def downgrade() -> None:
    with op.batch_alter_table("copy") as batch:
        batch.add_column(sa.Column("lent_to", sa.String(), nullable=True))
        batch.add_column(sa.Column("lent_on", sa.Date(), nullable=True))
