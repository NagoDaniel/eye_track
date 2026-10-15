from pathlib import Path

import pytest
from pydantic import ValidationError

from app.settings import Settings


def make(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "secret_key": "s" * 32,
        "postgres_password": "pw",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg, arg-type]


def test_defaults_match_plan_env_example() -> None:
    s = make()
    assert s.api_workers == 2
    assert s.worker_concurrency == 1
    assert s.onnx_provider == "cpu"
    assert s.max_upload_chunk_mb == 10
    assert s.log_level == "INFO"
    assert s.data_root == Path("/data")


def test_timing_defaults_match_plan() -> None:
    s = make()
    assert s.worker_heartbeat_interval_s == 30.0  # PLAN §11.1: heartbeat every 30 s
    assert s.worker_heartbeat_max_age_s == 90.0
    assert s.secret_key_min_length == 32


def test_short_secret_key_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 32 characters"):
        make(secret_key="s" * 31)
    assert make(secret_key="s" * 32).secret_key == "s" * 32


def test_missing_secrets_fail_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.mark.parametrize("field", ["secret_key", "postgres_password"])
def test_placeholder_secret_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match="change-me"):
        make(**{field: "change-me"})


def test_invalid_onnx_provider_rejected() -> None:
    with pytest.raises(ValidationError):
        make(onnx_provider="tpu")


def test_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    monkeypatch.setenv("POSTGRES_PASSWORD", "hunter2")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.log_level == "DEBUG"
    assert s.database_url == "postgresql+psycopg://eyetrack:hunter2@db:5432/eyetrack"


def test_database_url_escapes_password() -> None:
    s = make(postgres_password="p@ss/w:rd")
    assert "p%40ss%2Fw%3Ard" in s.database_url
