"""Open Library candidate-construction tests.

Covers the two real-world gotchas observed in production:
- OL splits comic-trade names across `title` + `subtitle`; the picker
  needs the combined string.
- OL's cover-by-isbn endpoint serves a 43-byte placeholder when no real
  cover exists. Appending ?default=false makes it 404 instead, so the
  cover downloader skips it cleanly.
"""

import httpx
import respx

from app.services import openlibrary


@respx.mock
def test_combines_title_and_subtitle_for_trades(anyio_backend=None):
    isbn = "9781506747828"
    respx.get("https://openlibrary.org/api/books").mock(
        return_value=httpx.Response(
            200,
            json={
                f"ISBN:{isbn}": {
                    "title": "Star Wars",
                    "subtitle": "Young Jedi Adventures--The Training Sessions",
                    "publishers": [{"name": "Dark Horse Comics"}],
                    "publish_date": "2025",
                    "number_of_pages": 48,
                    "key": "/books/OL60894116M",
                }
            },
        )
    )

    import asyncio

    candidates = asyncio.run(openlibrary.search_isbn(isbn))
    assert len(candidates) == 1
    c = candidates[0]
    assert c.title == "Star Wars: Young Jedi Adventures--The Training Sessions"
    assert c.publisher == "Dark Horse Comics"
    assert c.page_count == 48
    # Cover fallback uses default=false so a missing cover 404s instead of
    # serving the OL placeholder.
    assert c.cover_url is not None
    assert "default=false" in c.cover_url


@respx.mock
def test_uses_explicit_cover_when_present():
    isbn = "9780000111222"
    respx.get("https://openlibrary.org/api/books").mock(
        return_value=httpx.Response(
            200,
            json={
                f"ISBN:{isbn}": {
                    "title": "Hellboy",
                    "publishers": [{"name": "Dark Horse Comics"}],
                    "cover": {"large": "https://covers.openlibrary.org/b/id/6327883-L.jpg"},
                    "key": "/books/OLX",
                }
            },
        )
    )

    import asyncio

    [c] = asyncio.run(openlibrary.search_isbn(isbn))
    assert c.cover_url == "https://covers.openlibrary.org/b/id/6327883-L.jpg"
    assert "default=false" not in c.cover_url


def test_derive_series_strips_volume_markers():
    """Title-derived series so Open Library trade volumes group instead
    of scattering into one series per book."""
    from app.services.openlibrary import _derive_series
    assert _derive_series("Saga: Volume One") == "Saga"
    assert _derive_series("Saga, Volume Five") == "Saga"
    assert _derive_series("Saga, Book Two") == "Saga"
    assert _derive_series("Locke & Key, Vol. 1") == "Locke & Key"
    assert _derive_series("Immortal Hulk Vol. 3") == "Immortal Hulk"
    assert _derive_series("The Sandman Vol. 1: Preludes & Nocturnes") == "The Sandman"


def test_derive_series_returns_none_without_marker():
    """No volume marker -> None, so the title-as-series fallback applies
    and the user can assign the series by hand. Compendium / Omnibus
    editions are intentionally kept distinct from the numbered run."""
    from app.services.openlibrary import _derive_series
    assert _derive_series("Preludes & Nocturnes") is None
    assert _derive_series("Saga, Compendium One") is None
    assert _derive_series("Immortal Hulk Omnibus") is None
    assert _derive_series("Locke & Key") is None
    assert _derive_series(None) is None
    assert _derive_series("") is None


@respx.mock
def test_search_isbn_derives_series_when_ol_has_none():
    """End-to-end: an OL trade with no series field gets a derived one
    so a whole TPB run groups under a single series."""
    isbn = "9781607066019"
    respx.get("https://openlibrary.org/api/books").mock(
        return_value=httpx.Response(
            200,
            json={
                f"ISBN:{isbn}": {
                    "title": "Saga",
                    "subtitle": "Volume One",
                    "publishers": [{"name": "Image Comics"}],
                    "key": "/books/OLsaga1",
                }
            },
        )
    )
    import asyncio
    [c] = asyncio.run(openlibrary.search_isbn(isbn))
    assert c.title == "Saga: Volume One"
    assert c.series == "Saga"
