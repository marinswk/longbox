"""Merging Open Library + Google Books candidates for the same ISBN."""

from app.services.candidate_merge import merge_book_candidates
from app.services.schemas import CreatorRef, LookupCandidate


def _ol():
    return LookupCandidate(
        source="openlibrary", source_id="/books/OL1M",
        title="Saga: Volume One", isbn_13="9781607066019",
        publisher="Image Comics", cover_date="2012",
        cover_url="https://covers.openlibrary.org/b/id/123-L.jpg",
        creators=[CreatorRef(name="Fiona Staples", role="author")],
    )


def _gb():
    return LookupCandidate(
        source="googlebooks", source_id="GB123",
        title="Saga: Volume One", isbn_13="9781607066019",
        publisher="Image Comics", cover_date="2012-10-23",
        description="A long rich description from Google Books.",
        page_count=160, language="en", cover_url=None,
        creators=[CreatorRef(name="Brian K. Vaughan", role="author")],
    )


def test_merges_same_isbn_book_candidates():
    out = merge_book_candidates([_ol(), _gb()])
    assert len(out) == 1
    m = out[0]
    # Primary source is Google Books (richer); provenance recorded.
    assert m.source == "googlebooks"
    assert set(m.raw["merged_from"]) == {"openlibrary", "googlebooks"}
    # Best of each: GB's precise date + description, OL's real cover.
    assert m.cover_date == "2012-10-23"
    assert m.description.startswith("A long rich")
    assert m.cover_url.endswith("123-L.jpg")
    assert m.page_count == 160
    assert m.language == "en"
    # Authors unioned across both sources.
    names = {cr.name for cr in m.creators}
    assert names == {"Brian K. Vaughan", "Fiona Staples"}


def test_prefers_real_cover_over_placeholder():
    ol = _ol()
    ol.cover_url = "https://covers.openlibrary.org/b/isbn/x-L.jpg?default=false"  # placeholder
    gb = _gb()
    gb.cover_url = "https://books.google.com/books/content?id=GB123"
    [m] = merge_book_candidates([ol, gb])
    # The OL placeholder is skipped in favour of GB's real cover.
    assert m.cover_url == "https://books.google.com/books/content?id=GB123"


def test_singleton_book_candidate_passes_through():
    out = merge_book_candidates([_gb()])
    assert len(out) == 1 and out[0].source == "googlebooks"
    assert "merged_from" not in (out[0].raw or {})


def test_non_book_candidate_untouched():
    """A Wookieepedia ISBN candidate (rich SW comic record) is never
    merged with the book sources — it stays its own card."""
    wk = LookupCandidate(source="wookieepedia", source_id="Saga",
                         title="Saga", isbn_13="9781607066019")
    out = merge_book_candidates([wk, _ol(), _gb()])
    sources = [c.source for c in out]
    assert "wookieepedia" in sources
    # OL+GB merged into one, Wookieepedia separate → 2 total.
    assert len(out) == 2


def test_different_isbns_not_merged():
    a = _gb()
    b = _ol()
    b.isbn_13 = "9780000000000"
    out = merge_book_candidates([a, b])
    assert len(out) == 2
