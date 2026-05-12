"""Tests for the library view, plus the add-flow side effect of
creating Publisher + Series rows for use as filters."""

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> None:
    payload = {"title": "X"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200


def test_add_save_creates_publisher_and_series():
    with _client() as client:
        _save(
            client,
            title="Saga #2",
            isbn_13="9780000111001",
            series="Saga",
            publisher="Image Comics",
        )
        # second comic in same series re-uses rows
        _save(
            client,
            title="Saga #3",
            isbn_13="9780000111002",
            series="Saga",
            publisher="Image Comics",
        )
        body = client.get("/library").text
        assert "Image Comics" in body
        assert "Saga" in body


def test_library_page_renders_with_filters():
    with _client() as client:
        _save(
            client,
            title="Y #1",
            isbn_13="9780000222001",
            series="Y The Last Man",
            publisher="DC Vertigo",
            cover_date="2002-09-01",
        )
        _save(
            client,
            title="Y #2",
            isbn_13="9780000222002",
            series="Y The Last Man",
            publisher="DC Vertigo",
            cover_date="2002-10-01",
        )

        r = client.get("/library", params={"publisher": "DC Vertigo"})
        assert r.status_code == 200
        assert "Y The Last Man" in r.text

        r = client.get("/library", params={"q": "Y The Last Man"})
        assert r.status_code == 200
        assert "Y The Last Man" in r.text


def test_library_grid_partial_responds_to_facets():
    with _client() as client:
        _save(
            client,
            title="Z #1",
            isbn_13="9780000333001",
            series="Z Series",
            publisher="Z Publisher",
        )
        r = client.get("/library/grid", params={"publisher": "Z Publisher"})
        assert r.status_code == 200
        assert "Z Series" in r.text
        # No filter UI in the partial — only the grid + paginator.
        assert "FILTERS" not in r.text


def test_library_grouping_renders_section_headings():
    with _client() as client:
        _save(
            client,
            title="A #1",
            isbn_13="9780000444001",
            series="Alpha",
            publisher="Pub A",
        )
        _save(
            client,
            title="B #1",
            isbn_13="9780000444002",
            series="Beta",
            publisher="Pub B",
        )
        r = client.get("/library", params={"group": "publisher"})
        assert r.status_code == 200
        assert "Pub A" in r.text and "Pub B" in r.text


def test_library_empty_state_for_impossible_filter():
    with _client() as client:
        r = client.get("/library", params={"publisher": "Definitely Not A Publisher 9999"})
        assert r.status_code == 200
        assert "EMPTY LONGBOX" in r.text
