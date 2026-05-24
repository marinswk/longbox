"""Variant cover columns on Comic + Copy

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-24

Single issues often ship with many variant covers (the same WOTBH 5
issue has 17 distinct variants on Wookieepedia). Storage shape:

* ``Comic.cover_variants_json`` — JSON list ``[{"label", "url"}]`` that
  caches the article's cover gallery at save / refresh time. Used as
  the menu the "add another copy" form populates from, so the user
  doesn't re-fetch Wookieepedia per copy.
* ``Copy.variant_name`` + ``Copy.variant_cover_url`` — the specific
  variant the user owns for this physical copy. Both NULL means
  "standard cover" — render the Comic's main cover.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "comic",
        sa.Column("cover_variants_json", sa.String(), nullable=True),
    )
    op.add_column(
        "copy",
        sa.Column("variant_name", sa.String(), nullable=True),
    )
    op.add_column(
        "copy",
        sa.Column("variant_cover_url", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("copy", "variant_cover_url")
    op.drop_column("copy", "variant_name")
    op.drop_column("comic", "cover_variants_json")
