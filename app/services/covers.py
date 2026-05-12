"""Cover image downloader.

Saves remote cover URLs to `/data/covers/<sha1(url)>.<ext>` so the catalog
keeps working when the upstream source rotates URLs or goes away. Idempotent:
re-downloading the same URL is a no-op when the file already exists.

Returned path is the relative URL the app serves at (e.g. `/covers/<hash>.jpg`),
not a filesystem path.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings

log = logging.getLogger(__name__)

COVERS_DIRNAME = "covers"
URL_PREFIX = "/covers"

_EXT_BY_CONTENT_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


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

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.content
            ext = _ext_from_content_type(r.headers.get("content-type")) or _ext_from_url(url) or ".jpg"
    except Exception:
        log.warning("cover download failed for %s", url, exc_info=True)
        return None

    target = local_path_for(url, ext)
    target.write_bytes(data)
    return served_url_for(target)
