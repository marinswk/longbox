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
