"""ImportSession + ImportRow for the CSV import wizard

Revision ID: 0007
Revises: 0006
Create Date: 2026-05-09

The wizard persists state across HTTP requests so the user can leave and
come back. Each upload creates one ImportSession (keyed by an opaque
`token` in the URL) plus one ImportRow per parsed CSV row. Rows track
search results, the user's pick, and the eventual `Comic.id` after commit.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "importsession",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("column_map", sa.String(), nullable=True),  # JSON
        sa.Column("sources", sa.String(), nullable=True),     # JSON list
        sa.Column("config", sa.String(), nullable=True),      # JSON dict
        sa.Column("state", sa.String(), nullable=False, server_default="upload"),
    )
    op.create_index("ix_importsession_token", "importsession", ["token"], unique=True)

    op.create_table(
        "importrow",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "session_id", sa.Integer(),
            sa.ForeignKey("importsession.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("raw", sa.String(), nullable=False),       # JSON of original row
        sa.Column("mapped", sa.String(), nullable=True),     # JSON of normalized fields
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("candidates", sa.String(), nullable=True), # JSON list
        sa.Column("chosen_source", sa.String(), nullable=True),
        sa.Column("chosen_source_id", sa.String(), nullable=True),
        sa.Column("comic_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
    )
    op.create_index("ix_importrow_session_id", "importrow", ["session_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_importrow_session_id", table_name="importrow")
    op.drop_table("importrow")
    op.drop_index("ix_importsession_token", table_name="importsession")
    op.drop_table("importsession")
