"""Series.canceled_issues column

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-19

Some Wookieepedia series articles flag specific issues as cancelled
in their publication-date column (e.g. "Star Wars 3-D" has issues
1-3 published and 4-7 marked "Cancelled (planned for ...)"). Without
a parallel canceled-issues field the series shows 3/7 progress
forever even though every PUBLISHED issue is owned.

`canceled_issues` is a newline-separated list of article titles
that share storage shape with `expected_issues`. Progress
calculation subtracts canceled from the denominator; the series
detail page can render them in a separate faded section.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "series",
        sa.Column("canceled_issues", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("series", "canceled_issues")
