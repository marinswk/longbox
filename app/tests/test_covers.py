import httpx
import respx
from fastapi.testclient import TestClient

from app.main import create_app
from app.services import covers


def _client() -> TestClient:
    return TestClient(create_app())


def _real_png_bytes() -> bytes:
    """Generate a real 1×1 PNG via Pillow. Pillow's `verify()` (used
    by `covers._is_valid_image_payload`) is strict about CRCs, so a
    hand-crafted byte string with the right magic header but wrong
    CRC values won't pass; the test PNG needs to be Pillow-generated."""
    from io import BytesIO
    from PIL import Image
    buf = BytesIO()
    Image.new("RGBA", (1, 1), (255, 255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


PNG_BYTES = _real_png_bytes()


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
        return_value=httpx.Response(200, content=PNG_BYTES, headers={"content-type": "image/png"})
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


# ── Hardening: SSRF guard ─────────────────────────────────────────────


def test_is_safe_remote_host_blocks_loopback():
    assert covers._is_safe_remote_host("http://127.0.0.1/x.jpg") is False
    assert covers._is_safe_remote_host("http://[::1]/x.jpg") is False
    assert covers._is_safe_remote_host("http://localhost/x.jpg") is False


def test_is_safe_remote_host_blocks_private_ranges():
    """RFC1918 + link-local addresses are off-limits."""
    for url in [
        "http://10.0.0.1/x.jpg",
        "http://192.168.1.5/x.jpg",
        "http://172.16.0.1/x.jpg",
        "http://169.254.169.254/x.jpg",  # AWS metadata, classic SSRF target
    ]:
        assert covers._is_safe_remote_host(url) is False, url


def test_is_safe_remote_host_blocks_non_http_schemes():
    """`file://`, `gopher://`, etc. should never be fetched."""
    assert covers._is_safe_remote_host("file:///etc/passwd") is False
    assert covers._is_safe_remote_host("gopher://example/x") is False
    assert covers._is_safe_remote_host("ftp://example/x.jpg") is False


def test_is_safe_remote_host_accepts_public_literal_ip():
    """Literal-IP check doesn't need DNS — the test image's
    network stack might not have it."""
    assert covers._is_safe_remote_host("https://8.8.8.8/x.jpg") is True
    assert covers._is_safe_remote_host("https://1.1.1.1/x.jpg") is True


def test_is_safe_remote_host_allows_unresolvable_hostname():
    """Hostnames that fail DNS resolution pass through — let httpx
    surface the error rather than blocking pre-flight. Critical
    for keeping respx-mocked tests working in CI."""
    # `nonexistent.invalid` is reserved by RFC 6761 to always fail.
    assert covers._is_safe_remote_host("https://nonexistent.invalid/x.jpg") is True


def test_download_refuses_loopback_url(tmp_path, monkeypatch):
    """End-to-end: a `cover_url_remote` pointing at loopback never
    triggers an actual HTTP fetch. No request is sent; the function
    returns None and writes nothing to disk."""
    monkeypatch.setattr(covers.settings, "data_dir", tmp_path)
    import asyncio
    assert asyncio.run(covers.download("http://127.0.0.1:8080/cover.jpg")) is None
    covers_path = tmp_path / "covers"
    assert not covers_path.exists() or not list(covers_path.iterdir())


# ── Hardening: size cap + image validation ────────────────────────────


@respx.mock
def test_download_rejects_oversized_response(tmp_path, monkeypatch):
    """A malicious cover URL serving 50 MB+ must not be written."""
    monkeypatch.setattr(covers.settings, "data_dir", tmp_path)
    # Force the limit down so the test stays fast and doesn't need to
    # generate megabytes of bytes.
    monkeypatch.setattr(covers, "_MAX_COVER_BYTES", 1024)
    url = "https://covers.example/huge.png"
    huge = PNG_BYTES + b"\x00" * 4096  # well over the 1 KB ceiling
    respx.get(url).mock(
        return_value=httpx.Response(200, content=huge, headers={"content-type": "image/png"})
    )
    import asyncio
    assert asyncio.run(covers.download(url)) is None


@respx.mock
def test_download_rejects_non_image_payload(tmp_path, monkeypatch):
    """A server lying about content-type ('image/jpeg' but actual
    body is HTML) gets caught by the Pillow validation step."""
    monkeypatch.setattr(covers.settings, "data_dir", tmp_path)
    url = "https://covers.example/lie.jpg"
    respx.get(url).mock(
        return_value=httpx.Response(
            200,
            content=b"<!doctype html><body>not an image</body>",
            headers={"content-type": "image/jpeg"},
        )
    )
    import asyncio
    assert asyncio.run(covers.download(url)) is None
