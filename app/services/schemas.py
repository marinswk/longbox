"""Normalized lookup result returned by every metadata source."""

from typing import Any, Optional

from pydantic import BaseModel


class CreatorRef(BaseModel):
    """A person credited on a comic, with their role."""

    name: str
    role: Optional[str] = None


class LookupCandidate(BaseModel):
    source: str
    source_id: Optional[str] = None
    title: Optional[str] = None
    series: Optional[str] = None
    issue_number: Optional[str] = None
    publisher: Optional[str] = None
    cover_date: Optional[str] = None
    description: Optional[str] = None
    cover_url: Optional[str] = None
    isbn_10: Optional[str] = None
    isbn_13: Optional[str] = None
    upc: Optional[str] = None
    page_count: Optional[int] = None
    creators: list[CreatorRef] = []

    # Edition metadata.
    format: Optional[str] = None
    language: Optional[str] = None

    # Trade-only: newline-joined list of contained issues.
    collected_issues: Optional[str] = None

    # Star-Wars-specific buckets (null for non-SW comics).
    timeline: Optional[str] = None
    era: Optional[str] = None
    canon: Optional[str] = None  # "canon" | "legends" | None

    # Story arcs / crossovers ("War of the Bounty Hunters", etc.).
    story_arcs: list[str] = []

    # Characters appearing in the issue (CV `character_credits`,
    # Metron `characters`). Used to auto-create tags on save.
    characters: list[str] = []

    # Free-form upstream concept tags ("space opera", "war", etc. — CV
    # `concept_credits`). Empty for sources that don't expose them.
    concepts: list[str] = []

    raw: dict[str, Any] = {}
