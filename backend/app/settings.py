from pathlib import Path
from typing import Literal
from urllib.parse import quote

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PLACEHOLDER = "change-me"


class Settings(BaseSettings):
    """Backend configuration, read from the environment (see `.env.example`, PLAN §11.2)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    secret_key: str
    postgres_password: str
    postgres_user: str = "eyetrack"
    postgres_db: str = "eyetrack"
    postgres_host: str = "db"
    postgres_port: int = 5432

    data_root: Path = Path("/data")
    models_root: Path = Path("/models")

    api_workers: int = 2
    worker_concurrency: int = 1
    onnx_provider: Literal["cpu", "cuda"] = "cpu"
    max_upload_chunk_mb: int = 10
    log_level: str = "INFO"

    @field_validator("secret_key", "postgres_password")
    @classmethod
    def reject_placeholder(cls, value: str) -> str:
        if not value or value == PLACEHOLDER:
            raise ValueError(f"must be set to a real secret, not '{PLACEHOLDER}' or empty")
        return value

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{quote(self.postgres_user, safe='')}"
            f":{quote(self.postgres_password, safe='')}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
