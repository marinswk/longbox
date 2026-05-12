"""expand Comic with upc, source, format, language, SW timeline fields, collected_issues

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-29

Adds columns the metadata sources already carry but the schema dropped on
the floor:

  * upc                 — barcode for single issues (Wookieepedia)
  * source / source_id  — provenance, enabling refresh-from-source later
  * collected_issues    — newline-joined list, populated for trades
  * format              — softcover/hardcover/omnibus/etc. (Wookieepedia, CV)
  * language            — defaults to English when null
  * timeline / era / canon — Star Wars-specific buckets (null for non-SW)

Indexed columns: upc and source so refresh + duplicate-by-UPC stay cheap.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("comic") as batch:
        batch.add_column(sa.Column("upc", sa.String(), nullable=True))
        batch.add_column(sa.Column("source", sa.String(), nullable=True))
        batch.add_column(sa.Column("source_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("collected_issues", sa.String(), nullable=True))
        batch.add_column(sa.Column("format", sa.String(), nullable=True))
        batch.add_column(sa.Column("language", sa.String(), nullable=True))
        batch.add_column(sa.Column("timeline", sa.String(), nullable=True))
        batch.add_column(sa.Column("era", sa.String(), nullable=True))
        batch.add_column(sa.Column("canon", sa.String(), nullable=True))
    op.create_index("ix_comic_upc", "comic", ["upc"], unique=False)
    op.create_index("ix_comic_source", "comic", ["source"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_comic_source", table_name="comic")
    op.drop_index("ix_comic_upc", table_name="comic")
    with op.batch_alter_table("comic") as batch:
        batch.drop_column("canon")
        batch.drop_column("era")
        batch.drop_column("timeline")
        batch.drop_column("language")
        batch.drop_column("format")
        batch.drop_column("collected_issues")
        batch.drop_column("source_id")
        batch.drop_column("source")
        batch.drop_column("upc")
