"""Cover image downloader.

Saves remote cover URLs to `/data/covers/<sha1(url)>.<ext>` so the catalog
keeps working when the upstream source rotates URLs or goes away. Idempotent:
re-downloading the same URL is a no-op when the file already exists.

Returned path is the relative URL the app serves at (e.g. `/covers/<hash>.jpg`),
not a filesystem path.

Hardening (since v1.1.7):
  * `_MAX_COVER_BYTES` caps the download size — streamed so a 500 MB
    upstream response can't OOM the container.
  * `_is_safe_remote_host` blocks private / loopback / link-local IPs
    so a malicious cover URL can't be used to probe internal LAN hosts.
  * The downloaded bytes are validated as an actual image via Pillow
    (already a dependency) — a content-type lie ("image/jpeg" with HTML
    body) gets dropped on the floor.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import logging
import mimetypes
import socket
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from app.config import settings

log = logging.getLogger(__name__)

COVERS_DIRNAME = "covers"
URL_PREFIX = "/covers"

# 10 MB ceiling. Matches the upload-side limit in
# `routers/detail.py::upload_cover`. The real-world cover artwork on
# Wookieepedia / ComicVine / Metron is well under 1 MB; 10 MB is
# generous headroom without being a DoS vector.
_MAX_COVER_BYTES = 10 * 1024 * 1024

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _is_safe_remote_host(url: str) -> bool:
    """Refuse to fetch from private/loopback/link-local addresses.

    Stops a user-controlled `cover_url_remote` (settable from the
    confirm form, the edit form, or via `/api/comics` PATCH) from
    being weaponised as an SSRF probe of the LAN Longbox is hosted
    inside. The check resolves the URL's hostname and rejects if ANY
    of the returned addresses is in a private range.

    Public DNS that happens to resolve to a private address (DNS
    rebinding) is also caught because the resolution is part of the
    pre-fetch check. The actual HTTP fetch uses the same hostname
    so the resolver returning a public IP first and a private IP
    second is mostly mitigated, although a determined attacker
    could still race the resolver. Good-enough defense for the
    single-user threat model; not designed to thwart a serious
    attacker on the same LAN.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.hostname
    if not host:
        return False
    # Literal-IP URL: validate directly.
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if ip is not None:
        return not (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_multicast or ip.is_reserved or ip.is_unspecified)
    # Hostname: resolve and reject if ANY answer is private. Names
    # that fail to resolve are LET THROUGH — that lets httpx surface
    # the actual DNS error in the same shape an unguarded download
    # would have produced. The SSRF defense bites only when DNS
    # successfully resolves to a private address.
    try:
        addrs = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return True
    for entry in addrs:
        addr_str = entry[4][0]
        try:
            resolved = ipaddress.ip_address(addr_str)
        except ValueError:
            continue
        if (resolved.is_private or resolved.is_loopback
                or resolved.is_link_local or resolved.is_multicast
                or resolved.is_reserved or resolved.is_unspecified):
            return False
    return True


def _is_valid_image_payload(data: bytes) -> bool:
    """True iff `data` round-trips through Pillow as a real image
    (any format Pillow recognises). Content-type lies don't survive."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        return True
    except Exception:
        return False


def covers_dir() -> Path:
    path = settings.data_dir / COVERS_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _ext_from_url(url: str) -> Optional[str]:
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return None


def _ext_from_content_type(content_type: Optional[str]) -> Optional[str]:
    if not content_type:
        return None
    base = content_type.split(";", 1)[0].strip().lower()
    if base in _EXT_BY_CONTENT_TYPE:
        return _EXT_BY_CONTENT_TYPE[base]
    guess = mimetypes.guess_extension(base)
    return guess if guess in {".jpg", ".png", ".webp", ".gif"} else None


def local_path_for(url: str, ext: str) -> Path:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return covers_dir() / f"{digest}{ext}"


def served_url_for(local_path: Path) -> str:
    return f"{URL_PREFIX}/{local_path.name}"


def existing_local_url(url: str) -> Optional[str]:
    """Return the served URL if a cached file already exists for this remote URL."""
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    for candidate in covers_dir().glob(f"{digest}.*"):
        return served_url_for(candidate)
    return None


async def download(url: str) -> Optional[str]:
    if not url:
        return None

    cached = existing_local_url(url)
    if cached:
        return cached

    # SSRF guard: refuse to fetch from internal LAN / loopback.
    if not _is_safe_remote_host(url):
        log.warning("cover download blocked (unsafe host) for %s", url)
        return None

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            # `max_redirects` defaults to 20 — fine. We don't
            # disable follow_redirects because most CDNs 30x.
        ) as client:
            # Stream so we can cap the byte budget without ever
            # materialising a huge response in memory.
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                # Sanity-check the redirect target too (the initial
                # URL passed `_is_safe_remote_host`, but a redirect
                # could land on something internal).
                final_url = str(r.url)
                if final_url != url and not _is_safe_remote_host(final_url):
                    log.warning(
                        "cover download blocked (redirect to unsafe host): %s -> %s",
                        url, final_url,
                    )
                    return None
                chunks: list[bytes] = []
                size = 0
                async for chunk in r.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > _MAX_COVER_BYTES:
                        log.warning(
                            "cover download exceeded %d bytes for %s — aborting",
                            _MAX_COVER_BYTES, url,
                        )
                        return None
                data = b"".join(chunks)
                ext = (
                    _ext_from_content_type(r.headers.get("content-type"))
                    or _ext_from_url(url) or ".jpg"
                )
    except Exception:
        log.warning("cover download failed for %s", url, exc_info=True)
        return None

    # Validate the payload as an actual image — content-type lies
    # ("image/jpeg" with an HTML body) are dropped here.
    if not _is_valid_image_payload(data):
        log.warning("cover download rejected (not a valid image): %s", url)
        return None

    target = local_path_for(url, ext)
    target.write_bytes(data)
    return served_url_for(target)
