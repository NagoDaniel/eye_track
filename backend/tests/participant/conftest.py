"""Fixtures for the participant API tests (real PostgreSQL, real files in a temp `data_root`)."""

from collections.abc import Generator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db

from app.main import create_app
from app.models import Study, StudyMode, Video
from tests.dbutil import make_settings
from tests.participant.helpers import STUDY_CODE, MakeClient


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


@pytest.fixture
def make_client(migrated_engine: Engine, data_root: Path) -> Generator[MakeClient]:
    """Factory for clients on the migrated database; extra keyword args override settings."""
    with ExitStack() as stack:

        def make(**settings: Any) -> TestClient:
            app = create_app(make_settings(data_root=data_root, **settings), engine=migrated_engine)
            return stack.enter_context(TestClient(app))

        yield make


@pytest.fixture
def client(make_client: MakeClient) -> TestClient:
    return make_client()


@pytest.fixture
def study_id(migrated_engine: Engine) -> int:
    """A study with two videos in its playlist (video ids are given in reverse on purpose)."""
    with Db(migrated_engine) as db:
        v1 = Video(
            title="one",
            file_path="x",
            duration=10.5,
            width=1280,
            height=720,
            fps=25,
            sha256="a" * 64,
        )
        v2 = Video(
            title="two",
            file_path="y",
            duration=20.0,
            width=1280,
            height=720,
            fps=30,
            sha256="b" * 64,
        )
        db.add_all([v1, v2])
        db.flush()
        study = Study(
            name="Physics lecture study",
            code=STUDY_CODE,
            mode=StudyMode.REMOTE,
            config={"playlist": [str(v2.id), str(v1.id)], "consent_version": "v1"},
        )
        db.add(study)
        db.commit()
        return study.id
