import httpx
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import covers


def _client() -> TestClient:
    return TestClient(create_app())


PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x03\x00\x01\x5b\x9b\x05\xa4"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


@respx.mock
def test_download_writes_file_and_returns_served_url(tmp_path, monkeypatch):
    monkeypatch.setattr(covers.settings, "data_dir", tmp_path)
    url = "https://covers.example/x.png"
    respx.get(url).mock(
        return_value=httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})
    )

    import asyncio

    served = asyncio.run(covers.download(url))
    assert served and served.startswith("/covers/") and served.endswith(".png")

    files = list((tmp_path / "covers").iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == PNG_BYTES


@respx.mock
def test_download_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(covers.settings, "data_dir", tmp_path)
    url = "https://covers.example/y.jpg"
    route = respx.get(url).mock(
        return_value=httpx.Response(200, content=b"\xff\xd8\xff\xd9", headers={"content-type": "image/jpeg"})
    )

    import asyncio

    first = asyncio.run(covers.download(url))
    second = asyncio.run(covers.download(url))
    assert first == second
    assert route.call_count == 1


@respx.mock
def test_download_returns_none_on_upstream_error(tmp_path, monkeypatch):
    monkeypatch.setattr(covers.settings, "data_dir", tmp_path)
    url = "https://covers.example/missing.jpg"
    respx.get(url).mock(return_value=httpx.Response(404))

    import asyncio

    assert asyncio.run(covers.download(url)) is None


@respx.mock
def test_create_comic_schedules_cover_download(tmp_path, monkeypatch):
    monkeypatch.setattr(covers.settings, "data_dir", tmp_path)
    remote = "https://covers.example/saga.jpg"
    respx.get(remote).mock(
        return_value=httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})
    )

    with _client() as client:
        r = client.post(
            "/api/comics",
            json={"title": "Saga #1", "cover_url_remote": remote},
        )
        assert r.status_code == 201
        cid = r.json()["id"]

        r = client.get(f"/api/comics/{cid}")
        assert r.status_code == 200
        body = r.json()
        assert body["cover_url_remote"] == remote
        assert body["cover_url_local"] and body["cover_url_local"].startswith("/covers/")


@respx.mock
def test_refresh_cover_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(covers.settings, "data_dir", tmp_path)
    remote = "https://covers.example/refresh.jpg"
    respx.get(remote).mock(
        return_value=httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})
    )

    with _client() as client:
        cid = client.post("/api/comics", json={"title": "X", "cover_url_remote": remote}).json()["id"]
        r = client.post(f"/api/comics/{cid}/cover/refresh")
        assert r.status_code == 200
        assert r.json()["cover_url_local"].startswith("/covers/")


def test_refresh_cover_400_when_no_remote_url():
    with _client() as client:
        cid = client.post("/api/comics", json={"title": "no cover"}).json()["id"]
        r = client.post(f"/api/comics/{cid}/cover/refresh")
        assert r.status_code == 400
