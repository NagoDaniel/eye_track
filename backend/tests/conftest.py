"""Shared fixtures. DB tests run against real PostgreSQL 16 (never SQLite).

Set TEST_DATABASE_URL to use an existing server; otherwise a throwaway container is started.
Fails loudly if neither is available.
"""

import subprocess
import time
import uuid
from collections.abc import Generator

import pytest
from sqlalchemy import Engine, text

from app.db import make_engine

POSTGRES_IMAGE = "postgres:16"


def _docker(*args: str) -> str:
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str]:
    import os

    external = os.environ.get("TEST_DATABASE_URL")
    if external:
        yield external
        return
    name = f"eyetrack-test-{uuid.uuid4().hex[:8]}"
    _docker(
        "run", "-d", "--rm", "--name", name, "-p", "127.0.0.1::5432",
        "-e", "POSTGRES_PASSWORD=test", POSTGRES_IMAGE,
    )  # fmt: skip
    try:
        port = _docker("port", name, "5432/tcp").splitlines()[0].rsplit(":", 1)[1]
        url = f"postgresql+psycopg://postgres:test@127.0.0.1:{port}/postgres"
        engine = make_engine(url)
        for _ in range(60):
            try:
                with engine.connect() as conn:
                    conn.execute(text("select 1"))
                break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("test PostgreSQL did not become ready in 60 s")
        engine.dispose()
        yield url
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True)


@pytest.fixture
def engine(postgres_url: str) -> Generator[Engine]:
    """Engine on an empty `public` schema."""
    eng = make_engine(postgres_url)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield eng
    eng.dispose()


@pytest.fixture
def migrated_engine(engine: Engine) -> Engine:
    """Engine on a schema upgraded to the latest migration."""
    from tests.dbutil import upgrade

    upgrade(engine)
    return engine
