import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from app.db import make_engine
from app.logging import configure_logging
from app.main import create_app
from app.settings import Settings


def settings() -> Settings:
    return Settings(secret_key="s" * 32, postgres_password="pw", _env_file=None)  # type: ignore[call-arg]


def test_health_ok_when_database_reachable(migrated_engine: Engine) -> None:
    with TestClient(create_app(settings(), engine=migrated_engine)) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_health_503_when_database_down(
    capsys: pytest.CaptureFixture[str],
) -> None:
    dead = make_engine("postgresql+psycopg://u:p@127.0.0.1:1/db?connect_timeout=2")
    with TestClient(create_app(settings(), engine=dead)) as client:
        r = client.get("/api/health")
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "error": "database"}


def test_unknown_path_is_404(migrated_engine: Engine) -> None:
    with TestClient(create_app(settings(), engine=migrated_engine)) as client:
        assert client.get("/api/nope").status_code == 404


def test_requests_are_logged_as_json(
    migrated_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    with TestClient(create_app(settings(), engine=migrated_engine)) as client:
        client.get("/api/nope")
    records = [json.loads(x) for x in capsys.readouterr().err.splitlines() if x.startswith("{")]
    (rec,) = [r for r in records if r.get("path") == "/api/nope"]
    assert rec["method"] == "GET" and rec["status"] == 404
    assert isinstance(rec["duration_ms"], float)
    logging.getLogger().handlers.clear()


def test_health_polls_not_logged_at_info(
    migrated_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    with TestClient(create_app(settings(), engine=migrated_engine)) as client:
        client.get("/api/health")
    records = [json.loads(x) for x in capsys.readouterr().err.splitlines() if x.startswith("{")]
    assert [r for r in records if r["logger"] == "app.access"] == []
    logging.getLogger().handlers.clear()
