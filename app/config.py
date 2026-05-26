from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "production"
    data_dir: Path = Path("/data")

    comicvine_api_key: Optional[str] = None
    comicvine_user_agent: str = "Longbox/1.1 (+https://github.com/marinswk/longbox)"

    metron_user: Optional[str] = None
    metron_pass: Optional[str] = None

    metadata_cache_ttl_days: int = 30

    # Comma-separated allowlist for Starlette's TrustedHostMiddleware.
    # `*` accepts anything (default, matches the LAN-only deploy
    # model). Tighten when fronting Longbox with a reverse proxy:
    # ``ALLOWED_HOSTS=longbox.example.com,localhost``.
    allowed_hosts: str = "*"

    # Comma-separated allowlist of origins for non-GET requests. When
    # set, any non-GET whose `Origin` or `Referer` doesn't match one
    # of these (or the request `Host`) is rejected with 403. Acts as
    # a lightweight CSRF guard for the no-auth LAN deploy: a malicious
    # site can't trick the browser into POSTing to `/admin/wipe`
    # because the cross-site `Origin` won't be on the list.
    # Empty (default) = disabled — useful for first-run / local dev.
    # Typical deployment value: the URLs you actually open Longbox at,
    # e.g. ``http://longbox.lan:8080,https://longbox.lan``.
    csrf_allowed_origins: str = ""


settings = Settings()
