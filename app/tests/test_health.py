from fastapi.testclient import TestClient

from app.main import create_app
from app.version import __version__


def _client() -> TestClient:
    return TestClient(create_app())


def test_health():
    # /health is mounted on the bare app and doesn't need DB tables, so
    # lifespan-less invocation is fine here.
    with _client() as client:
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "version": __version__}


def test_index_renders_landing_for_empty_library():
    with _client() as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "bg-ink" in r.text
        assert "text-crawl" in r.text
        assert "LONGBOX" in r.text


def test_index_loaded_state_shows_counts_and_recent():
    """Once a comic exists, the home page swaps the onboarding strip for
    the live counts + recent additions panel."""
    with _client() as client:
        r = client.post(
            "/add/save",
            data={"title": "Home Page Test #1", "isbn_13": "9789999999991",
                  "publisher": "P", "series": "S"},
        )
        assert r.status_code == 200

        r = client.get("/")
        assert r.status_code == 200
        # Loaded state markers.
        assert "RECENT ADDITIONS" in r.text
        assert "Home Page Test #1" in r.text
        # The empty-state copy is gone.
        assert "Time to scan" not in r.text
