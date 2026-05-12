"""initial schema (baseline matching SQLModel.metadata as of Phase 11b)

Revision ID: 0001
Revises:
Create Date: 2026-04-28

This migration captures the schema previously created via
`SQLModel.metadata.create_all`. On an existing volume DB the lifespan
stamps `head` without running `upgrade`, so this script only ever executes
on a fresh install. Subsequent migrations carry real schema changes.
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "publisher",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_publisher_name", "publisher", ["name"], unique=True)
    op.create_index("ix_publisher_slug", "publisher", ["slug"], unique=True)

    op.create_table(
        "series",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("publisher_id", sa.Integer(), nullable=True),
        sa.Column("start_year", sa.Integer(), nullable=True),
        sa.Column("end_year", sa.Integer(), nullable=True),
        sa.Column("fandom", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("cover_url", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["publisher_id"], ["publisher.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_series_name", "series", ["name"], unique=False)
    op.create_index("ix_series_publisher_id", "series", ["publisher_id"], unique=False)
    op.create_index("ix_series_fandom", "series", ["fandom"], unique=False)

    op.create_table(
        "comic",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("series_id", sa.Integer(), nullable=True),
        sa.Column("issue_number", sa.String(), nullable=True),
        sa.Column("variant", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("cover_date", sa.Date(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("isbn_10", sa.String(), nullable=True),
        sa.Column("isbn_13", sa.String(), nullable=True),
        sa.Column("comicvine_id", sa.String(), nullable=True),
        sa.Column("metron_id", sa.String(), nullable=True),
        sa.Column("marvel_id", sa.String(), nullable=True),
        sa.Column("cover_url_local", sa.String(), nullable=True),
        sa.Column("cover_url_remote", sa.String(), nullable=True),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("cover_price_eur", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["series_id"], ["series.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_comic_series_id", "comic", ["series_id"], unique=False)
    op.create_index("ix_comic_issue_number", "comic", ["issue_number"], unique=False)
    op.create_index("ix_comic_title", "comic", ["title"], unique=False)
    op.create_index("ix_comic_isbn_10", "comic", ["isbn_10"], unique=False)
    op.create_index("ix_comic_isbn_13", "comic", ["isbn_13"], unique=False)
    op.create_index("ix_comic_comicvine_id", "comic", ["comicvine_id"], unique=False)
    op.create_index("ix_comic_metron_id", "comic", ["metron_id"], unique=False)
    op.create_index("ix_comic_marvel_id", "comic", ["marvel_id"], unique=False)

    op.create_table(
        "copy",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("comic_id", sa.Integer(), nullable=False),
        sa.Column("condition", sa.String(), nullable=True),
        sa.Column("storage_location", sa.String(), nullable=True),
        sa.Column("price_paid_eur", sa.Float(), nullable=True),
        sa.Column("purchase_date", sa.Date(), nullable=True),
        sa.Column("notes", sa.String(), nullable=True),
        sa.Column("read_status", sa.String(), nullable=True),
        sa.Column("date_read", sa.Date(), nullable=True),
        sa.Column("lent_to", sa.String(), nullable=True),
        sa.Column("lent_on", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["comic_id"], ["comic.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_copy_comic_id", "copy", ["comic_id"], unique=False)
    op.create_index("ix_copy_read_status", "copy", ["read_status"], unique=False)

    op.create_table(
        "creator",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_creator_name", "creator", ["name"], unique=True)

    op.create_table(
        "comiccreator",
        sa.Column("comic_id", sa.Integer(), nullable=False),
        sa.Column("creator_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["comic_id"], ["comic.id"]),
        sa.ForeignKeyConstraint(["creator_id"], ["creator.id"]),
        sa.PrimaryKeyConstraint("comic_id", "creator_id", "role"),
    )

    op.create_table(
        "character",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_character_name", "character", ["name"], unique=True)

    op.create_table(
        "comiccharacter",
        sa.Column("comic_id", sa.Integer(), nullable=False),
        sa.Column("character_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["comic_id"], ["comic.id"]),
        sa.ForeignKeyConstraint(["character_id"], ["character.id"]),
        sa.PrimaryKeyConstraint("comic_id", "character_id"),
    )

    op.create_table(
        "storyarc",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_storyarc_name", "storyarc", ["name"], unique=True)

    op.create_table(
        "comicarc",
        sa.Column("comic_id", sa.Integer(), nullable=False),
        sa.Column("arc_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["comic_id"], ["comic.id"]),
        sa.ForeignKeyConstraint(["arc_id"], ["storyarc.id"]),
        sa.PrimaryKeyConstraint("comic_id", "arc_id"),
    )

    op.create_table(
        "tag",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tag_name", "tag", ["name"], unique=True)

    op.create_table(
        "comictag",
        sa.Column("comic_id", sa.Integer(), nullable=False),
        sa.Column("tag_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["comic_id"], ["comic.id"]),
        sa.ForeignKeyConstraint(["tag_id"], ["tag.id"]),
        sa.PrimaryKeyConstraint("comic_id", "tag_id"),
    )

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

    op.create_table(
        "metadatacache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("payload", sa.String(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_metadatacache_source", "metadatacache", ["source"], unique=False)
    op.create_index("ix_metadatacache_key", "metadatacache", ["key"], unique=False)


def downgrade() -> None:
    op.drop_table("metadatacache")
    op.drop_table("pulllist")
    op.drop_table("wishlist")
    op.drop_table("comictag")
    op.drop_table("tag")
    op.drop_table("comicarc")
    op.drop_table("storyarc")
    op.drop_table("comiccharacter")
    op.drop_table("character")
    op.drop_table("comiccreator")
    op.drop_table("creator")
    op.drop_table("copy")
    op.drop_table("comic")
    op.drop_table("series")
    op.drop_table("publisher")
