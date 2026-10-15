from pathlib import Path
from typing import Literal, Self
from urllib.parse import quote

from pydantic import Field, field_validator, model_validator
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
    max_video_upload_mb: int = Field(default=2048, gt=0)
    log_level: str = "INFO"

    # Admin login (PLAN §6, §10). Cookie is HTTPS-only unless disabled for plain-http local dev.
    admin_cookie_secure: bool = True
    admin_session_max_age_s: int = 12 * 3600
    admin_min_password_length: int = 12
    # Signs admin cookies and CSRF tokens; a short key is guessable.
    secret_key_min_length: int = 32

    # Participant API limits (PLAN §6, §10; DECISIONS 1b item 9, 10). Sizes are in MiB / bytes.
    upload_token_max_age_s: int = 24 * 3600
    max_session_total_mb: int = 2048
    client_error_max_bytes: int = 16384
    # Session creations plus client-error reports per client IP per hour, per API process.
    rate_limit_per_hour: int = 300
    # Most chunks per stream a session may report in /complete (stops a huge `missing` list).
    # 2 s chunks: 100 000 chunks is more than 50 hours.
    max_chunks_per_stream: int = 100_000
    # Most one gzip-compressed events chunk may inflate to (a few KB of gzip can inflate to GBs).
    max_events_chunk_inflated_mb: int = 32

    # Worker timing (PLAN §11.1: heartbeat every 30 s).
    worker_heartbeat_interval_s: float = 30.0
    worker_heartbeat_max_age_s: float = 90.0
    worker_poll_interval_s: float = 2.0

    @field_validator("secret_key", "postgres_password")
    @classmethod
    def reject_placeholder(cls, value: str) -> str:
        if not value or value == PLACEHOLDER:
            raise ValueError(f"must be set to a real secret, not '{PLACEHOLDER}' or empty")
        return value

    @model_validator(mode="after")
    def check_secret_key_length(self) -> Self:
        if len(self.secret_key) < self.secret_key_min_length:
            raise ValueError(
                f"SECRET_KEY must be at least {self.secret_key_min_length} characters "
                "(scripts/setup.sh generates a suitable one)"
            )
        return self

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{quote(self.postgres_user, safe='')}"
            f":{quote(self.postgres_password, safe='')}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )
