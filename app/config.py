from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "production"
    data_dir: Path = Path("/data")

    comicvine_api_key: Optional[str] = None
    comicvine_user_agent: str = "Longbox/0.1 (+https://github.com/local/longbox)"

    metron_user: Optional[str] = None
    metron_pass: Optional[str] = None

    metadata_cache_ttl_days: int = 30


settings = Settings()
