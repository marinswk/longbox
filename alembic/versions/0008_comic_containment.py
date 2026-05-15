"""ComicContainment: parent comic (omnibus / TPB) contains child comics

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-15

A many-to-many link table where one Comic ("parent" — typically an
omnibus or larger collection) references other Comics ("children" —
the TPBs / volumes it collects). Both sides are Comic rows; the
child rows can exist as full library entries with copies OR as
stubs (no Copy attached) representing tracked-but-not-owned books.

The relationship is intentionally generic — nothing constrains it to
omnibus → TPB pairs — so future use cases (e.g. trade collects
single issues you've stubbed in) work without schema changes.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "comiccontainment",
        sa.Column("parent_id", sa.Integer(), sa.ForeignKey("comic.id"),
                  nullable=False, primary_key=True),
        sa.Column("child_id", sa.Integer(), sa.ForeignKey("comic.id"),
                  nullable=False, primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_comiccontainment_parent", "comiccontainment", ["parent_id"],
    )
    op.create_index(
        "ix_comiccontainment_child", "comiccontainment", ["child_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_comiccontainment_child", table_name="comiccontainment")
    op.drop_index("ix_comiccontainment_parent", table_name="comiccontainment")
    op.drop_table("comiccontainment")
