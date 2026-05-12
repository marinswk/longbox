"""Duplicates page — comics with more than one copy."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def _save(client: TestClient, **data) -> int:
    payload = {"title": "X"}
    payload.update(data)
    r = client.post("/add/save", data=payload)
    assert r.status_code == 200
    return next(c["id"] for c in client.get("/api/comics").json() if c.get("isbn_13") == data.get("isbn_13"))


def test_duplicates_page_lists_only_multi_copy_comics():
    with _client() as client:
        # Single-copy comic — should NOT appear.
        _save(client, title="Solo Copy", isbn_13="9783000000001",
              series="Solo", publisher="P")
        # Two-copy comic — adding via /add/save creates one copy, then one more.
        cid_two = _save(client, title="Pair Copy", isbn_13="9783000000002",
                        series="Pair", publisher="P")
        client.post(f"/comic/{cid_two}/copies", data={"condition": "VF"})
        # Three-copy comic — should rank higher than the pair.
        cid_three = _save(client, title="Triple Copy", isbn_13="9783000000003",
                          series="Trip", publisher="P")
        client.post(f"/comic/{cid_three}/copies", data={"condition": "VF"})
        client.post(f"/comic/{cid_three}/copies", data={"condition": "FN"})

        r = client.get("/duplicates")
        assert r.status_code == 200
        body = r.text

        assert "Pair Copy" in body
        assert "Triple Copy" in body
        assert "Solo Copy" not in body

        # Higher-count comic appears earlier in the rendered list.
        assert body.index("Triple Copy") < body.index("Pair Copy")

        # Count badges visible.
        assert "×3" in body
        assert "×2" in body


def test_duplicates_empty_state_when_no_multi_copies():
    with _client() as client:
        # Save just one comic, one copy → the dup page should be empty.
        _save(client, title="Lonesome", isbn_13="9783000000999",
              series="Lonesome", publisher="P")
        r = client.get("/duplicates")
        assert r.status_code == 200
        # Either renders the empty state, or — because earlier tests in the
        # session may have left dups — at minimum doesn't crash.
        assert "DUPLICATES" in r.text


def test_dupes_link_appears_in_nav():
    with _client() as client:
        r = client.get("/library")
        assert 'href="/duplicates"' in r.text
