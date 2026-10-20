"""Shared fixtures. DB tests run against real PostgreSQL 16 (never SQLite).

Set TEST_DATABASE_URL to use an existing server; otherwise a throwaway container is started.
Fails loudly if neither is available.
"""

import os
import subprocess
import sys
import time
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import Engine, text

from app.db import make_engine
from tests.session_fixture import FixtureSession

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


@pytest.fixture(scope="session")
def face_model(request: pytest.FixtureRequest) -> Path:
    """The MediaPipe Face Landmarker model (`shared/models.json`, sha256 checked).

    `FACE_LANDMARKER_MODEL` (set in the backend image) is used when it points to a file; otherwise
    the model is downloaded once into pytest's cache. Fails loudly when neither works.
    """
    configured = os.environ.get("FACE_LANDMARKER_MODEL")
    if configured and Path(configured).is_file():
        return Path(configured)
    shared = Path(__file__).resolve().parents[2] / "shared"
    sys.path.insert(0, str(shared))
    import download_model

    assert request.config.cache is not None
    cache = request.config.cache.mkdir("models")
    download_model.fetch(download_model.DEFAULT_MANIFEST, ["face_landmarker"], cache)
    return cache / "face_landmarker.task"


@pytest.fixture(scope="session")
def model_b_root(request: pytest.FixtureRequest) -> Path:
    """Directory with the small Pipeline B weights (`l2cs_mobileone_s0`, `shared/models.json`).

    `L2CS_MODEL` (path of `mobileone_s0_gaze.onnx`) is used when it points to a file; otherwise the
    weights are downloaded once into pytest's cache (sha256 checked). Fails loudly when neither
    works.
    """
    configured = os.environ.get("L2CS_MODEL")
    if configured and Path(configured).is_file():
        return Path(configured).parent
    shared = Path(__file__).resolve().parents[2] / "shared"
    sys.path.insert(0, str(shared))
    import download_model

    assert request.config.cache is not None
    cache = request.config.cache.mkdir("models")
    download_model.fetch(download_model.DEFAULT_MANIFEST, ["l2cs_mobileone_s0"], cache)
    return cache


@pytest.fixture(autouse=True)
def no_git_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    """`make` exports GIT_SHA for the image build; tests must not depend on it (the ones that
    need it set it themselves)."""
    monkeypatch.delenv("GIT_SHA", raising=False)


@pytest.fixture(scope="session")
def b_session(
    tmp_path_factory: pytest.TempPathFactory, face_model: Path, model_b_root: Path
) -> FixtureSession:
    """The recorded session after the A pipeline and Pipeline B (real MediaPipe, real small gaze
    network), loose inclusion thresholds. Read-only: tests that change files copy it first."""
    from tests.analysis_data import add_pipeline_b, process_fixture

    s = process_fixture(tmp_path_factory.mktemp("b_session"), face_model)
    add_pipeline_b(s, model_b_root)
    return s
