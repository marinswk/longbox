"""Auto-tag refinements:

* Concepts are dropped (CV `concept_credits` is too noisy).
* Character names have trailing parenthetical disambiguators stripped.
* Auto-tag UI surfaces a flash message indicating what happened.
* Wookieepedia parses the `==Appearances==` section to populate characters.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import SessionLocal
from app.main import create_app
from app.models import Comic, ComicTag, Tag
from app.services.schemas import LookupCandidate
from app.services.wookieepedia import _extract_appearances_characters
from app.routers.detail import _clean_character_name


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X", "publisher": "Test Publisher", "series": "Test Series"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    comics = client.get("/api/comics", params={"limit": 500}).json()
    return next(c["id"] for c in comics if c.get("isbn_13") == data.get("isbn_13"))


def _set_source(comic_id: int, source: str, source_id: str):
    async def _go():
        async with SessionLocal() as session:
            c = await session.get(Comic, comic_id)
            c.source, c.source_id = source, source_id
            session.add(c)
            await session.commit()
    asyncio.run(_go())


def _tags_for(comic_id: int) -> set[str]:
    async def _go():
        async with SessionLocal() as session:
            rows = (await session.exec(
                select(Tag.name).join(ComicTag, ComicTag.tag_id == Tag.id)
                .where(ComicTag.comic_id == comic_id)
            )).all()
            return set(rows)
    return asyncio.run(_go())


# ── Pure helpers ──────────────────────────────────────────────────────────

def test_clean_character_name_strips_trailing_paren():
    assert _clean_character_name("Han Solo (Earth-616)") == "Han Solo"
    assert _clean_character_name("Boba Fett (Star Wars)") == "Boba Fett"
    assert _clean_character_name("Han Solo") == "Han Solo"
    # Mid-name parens are kept (genuine names).
    assert _clean_character_name("R2-D2") == "R2-D2"


def test_appearances_parser_extracts_characters_subsection():
    wikitext = """
==Plot==
Some plot.

==Appearances==
===Characters===
*[[Han Solo]]
*[[Boba Fett|Boba Fett (bounty hunter)]]
*[[Chewbacca]]

===Vehicles===
*[[Millennium Falcon]]

==Behind the scenes==
Some trivia.
"""
    chars = _extract_appearances_characters(wikitext)
    assert "Han Solo" in chars
    assert "Boba Fett (bounty hunter)" in chars
    assert "Chewbacca" in chars
    # Vehicles must NOT leak in when a Characters subsection is present.
    assert "Millennium Falcon" not in chars


def test_appearances_parser_falls_back_when_no_subsections():
    """Some articles use `==Appearances==` without a Characters subsection
    — capture every wikilink under it as a best-effort fallback."""
    wikitext = """
==Appearances==
*[[Luke Skywalker]]
*[[Leia Organa]]
*[[Tatooine]]

==References==
foo
"""
    chars = _extract_appearances_characters(wikitext)
    assert "Luke Skywalker" in chars
    assert "Leia Organa" in chars


def test_appearances_parser_returns_empty_when_no_section():
    assert _extract_appearances_characters("plain text, nothing here") == []


# ── End-to-end auto-tag endpoint ─────────────────────────────────────────

def test_auto_tag_drops_concepts_and_strips_paren_disambiguators(monkeypatch):
    async def fake_refetch(source: str, source_id: str) -> Optional[LookupCandidate]:
        # Mimic the real _refetch_candidate — return None when not actually
        # linked, so the save flow doesn't auto-tag before we set a source.
        if not source or not source_id:
            return None
        return LookupCandidate(
            source="comicvine", source_id=source_id,
            characters=["Han Solo (Earth-616)", "Boba Fett"],
            story_arcs=["War of the Bounty Hunters"],
            concepts=["bounty hunting", "blaster"],  # MUST NOT be applied
        )
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    with _client() as client:
        cid = _save(client, title="Polish #1", isbn_13="9793000000001",
                    series="Polish Series")
        _set_source(cid, "comicvine", "1234")

        r = client.post(f"/comic/{cid}/auto-tag")
        assert r.status_code == 200

        names = _tags_for(cid)
        assert "chars: han solo" in names
        assert "chars: boba fett" in names
        # Original parenthetical version not stored.
        assert "chars: han solo (earth-616)" not in names
        # Concepts dropped.
        assert "bounty hunting" not in names
        assert "blaster" not in names
        # Arcs kept.
        assert "war of the bounty hunters" in names


def test_auto_tag_flash_when_no_source_set():
    with _client() as client:
        cid = _save(client, title="Polish NoSrc", isbn_13="9793000000099",
                    series="Polish NoSrc Series")
        r = client.post(f"/comic/{cid}/auto-tag")
        assert r.status_code == 200
        assert "No source linked" in r.text


def test_auto_tag_flash_reports_added_count(monkeypatch):
    async def fake_refetch(source: str, source_id: str) -> Optional[LookupCandidate]:
        if not source or not source_id:
            return None
        return LookupCandidate(
            source="comicvine", source_id=source_id,
            characters=["Mara Jade"],
            story_arcs=["Heir to the Empire"],
        )
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    with _client() as client:
        cid = _save(client, title="Polish Flash", isbn_13="9793000000201",
                    series="Polish Flash Series")
        _set_source(cid, "comicvine", "5555")
        r = client.post(f"/comic/{cid}/auto-tag")
        assert r.status_code == 200
        # 2 tags should be added on the first run.
        assert "Added 2 tag" in r.text


def test_auto_tag_flash_when_upstream_returns_no_data(monkeypatch):
    async def fake_refetch(source: str, source_id: str) -> Optional[LookupCandidate]:
        if not source or not source_id:
            return None
        return LookupCandidate(source="comicvine", source_id=source_id)
    monkeypatch.setattr("app.routers.add._refetch_candidate", fake_refetch)

    with _client() as client:
        cid = _save(client, title="Polish Empty", isbn_13="9793000000301",
                    series="Polish Empty Series")
        _set_source(cid, "comicvine", "0")
        r = client.post(f"/comic/{cid}/auto-tag")
        assert "no characters or arcs to tag" in r.text
