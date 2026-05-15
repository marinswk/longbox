from datetime import UTC, date, datetime
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Publisher(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    slug: str = Field(index=True, unique=True)


class Series(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    publisher_id: Optional[int] = Field(default=None, foreign_key="publisher.id", index=True)
    start_year: Optional[int] = None
    end_year: Optional[int] = None
    description: Optional[str] = None
    cover_url: Optional[str] = None

    # Provenance for refreshing the issue list. Same shape as Comic.source.
    source: Optional[str] = Field(default=None, index=True)
    source_id: Optional[str] = None

    # Newline-joined list of issue article titles (e.g. "Jedi Knights 1\n
    # Jedi Knights 2\n…"). Used by the missing-issues detector to compare
    # against the comics owned for this series.
    expected_issues: Optional[str] = None


class Comic(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    series_id: Optional[int] = Field(default=None, foreign_key="series.id", index=True)

    issue_number: Optional[str] = Field(default=None, index=True)
    variant: Optional[str] = None
    title: Optional[str] = Field(default=None, index=True)
    cover_date: Optional[date] = None
    page_count: Optional[int] = None

    isbn_10: Optional[str] = Field(default=None, index=True)
    isbn_13: Optional[str] = Field(default=None, index=True)
    upc: Optional[str] = Field(default=None, index=True)

    # Universe / brand the comic belongs to ("star wars", "aggretsuko",
    # "locke & key"). Stored lowercase, normalized whitespace. Free-form
    # because new fandoms appear over time.
    fandom: Optional[str] = Field(default=None, index=True)
    comicvine_id: Optional[str] = Field(default=None, index=True)
    metron_id: Optional[str] = Field(default=None, index=True)
    marvel_id: Optional[str] = Field(default=None, index=True)

    # Provenance — lets the detail page offer a "refresh from source" action.
    source: Optional[str] = Field(default=None, index=True)
    source_id: Optional[str] = None

    # Trade/collection-only — newline-joined list of the contained issues.
    collected_issues: Optional[str] = None

    # Edition metadata. `format` covers softcover/hardcover/omnibus/etc.
    # `language` falls back to "English" implicitly when null.
    format: Optional[str] = None
    language: Optional[str] = None

    # Star-Wars-specific (kept null for non-SW comics): in-universe date,
    # broad era ("Imperial", "New Republic"), and canon vs Legends bucket.
    timeline: Optional[str] = None
    era: Optional[str] = None
    canon: Optional[str] = None  # "canon" | "legends" | None

    cover_url_local: Optional[str] = None
    cover_url_remote: Optional[str] = None
    description: Optional[str] = None
    cover_price_eur: Optional[float] = None

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Copy(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    comic_id: int = Field(foreign_key="comic.id", index=True)
    condition: Optional[str] = None
    storage_location: Optional[str] = None
    price_paid_eur: Optional[float] = None
    purchase_date: Optional[date] = None
    notes: Optional[str] = None
    read_status: Optional[str] = Field(default=None, index=True)
    date_read: Optional[date] = None


class Creator(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)


class ComicCreator(SQLModel, table=True):
    comic_id: int = Field(foreign_key="comic.id", primary_key=True)
    creator_id: int = Field(foreign_key="creator.id", primary_key=True)
    role: str = Field(primary_key=True)


class Character(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)


class ComicCharacter(SQLModel, table=True):
    comic_id: int = Field(foreign_key="comic.id", primary_key=True)
    character_id: int = Field(foreign_key="character.id", primary_key=True)


class StoryArc(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)


class ComicArc(SQLModel, table=True):
    comic_id: int = Field(foreign_key="comic.id", primary_key=True)
    arc_id: int = Field(foreign_key="storyarc.id", primary_key=True)


class Tag(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)


class ComicTag(SQLModel, table=True):
    comic_id: int = Field(foreign_key="comic.id", primary_key=True)
    tag_id: int = Field(foreign_key="tag.id", primary_key=True)


class ComicContainment(SQLModel, table=True):
    """A Comic (`parent`) collects another Comic (`child`). Typically
    used for omnibus → TPB relationships, but the link is intentionally
    generic so trade → individual-issue references and similar use
    cases work without further schema changes. Children can be
    library-owned (with Copy rows) or stubs (zero Copies, representing
    a tracked-but-not-owned book referenced from an owned parent)."""
    parent_id: int = Field(foreign_key="comic.id", primary_key=True)
    child_id: int = Field(foreign_key="comic.id", primary_key=True)
    position: int = Field(default=0)
    created_at: datetime = Field(default_factory=_utcnow)


class MetadataCache(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    key: str = Field(index=True)
    payload: str
    fetched_at: datetime = Field(default_factory=_utcnow)


# ---------------------------------------------------------------------------
# CSV import wizard
# ---------------------------------------------------------------------------


class ImportSession(SQLModel, table=True):
    """One row per CSV uploaded via the import wizard. Identified by an
    opaque URL `token` so users can come back to a half-finished import.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    token: str = Field(index=True, unique=True)
    filename: Optional[str] = None
    created_at: datetime = Field(default_factory=_utcnow)
    column_map: Optional[str] = None  # JSON: {our_field: csv_header}
    sources: Optional[str] = None      # JSON list ["wookieepedia", ...]
    config: Optional[str] = None       # JSON dict (year_tolerance, etc.)
    state: str = Field(default="upload")  # upload|map|config|resolve|done


class ImportRow(SQLModel, table=True):
    """One row per parsed CSV entry inside an ImportSession. Stores the
    original CSV row, the user's mapped/normalized fields, the candidate
    search hits we found, the user's pick, and the eventual `Comic.id`.
    """

    id: Optional[int] = Field(default=None, primary_key=True)
    session_id: int = Field(foreign_key="importsession.id", index=True)
    row_index: int
    raw: str               # JSON of the original CSV row dict
    mapped: Optional[str] = None       # JSON of normalized fields
    status: str = Field(default="pending")
    # Statuses: pending | matched | multi | not_found | skipped | committed | errored
    candidates: Optional[str] = None   # JSON list of candidate hits
    chosen_source: Optional[str] = None
    chosen_source_id: Optional[str] = None
    comic_id: Optional[int] = None
    error: Optional[str] = None
