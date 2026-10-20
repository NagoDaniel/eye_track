"""`cli.py process-b`: run Pipeline B for a session here, or queue it for a whole study."""

import logging
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as Db
from typer.testing import CliRunner

import cli
from app.db import make_session_factory
from app.models import Job, JobKind, JobStatus, SessionStatus
from app.models import Session as SessionRow
from app.pipeline_b import STAGES_B_FILE
from app.storage import derived_dir
from tests.dbutil import make_settings
from tests.session_db import seed_session
from tests.session_fixture import FixtureSession

runner = CliRunner()
MODEL = "l2cs_mobileone_s0"


@pytest.fixture(autouse=True)
def no_leftover_log_handlers() -> Iterator[None]:
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def here(
    migrated_engine: Engine,
    a_session: FixtureSession,
    tmp_path: Path,
    model_b_root: Path,
    face_model: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> FixtureSession:
    root = tmp_path / "data"
    shutil.copytree(a_session.root, root)
    s = FixtureSession(
        root, a_session.study_id, a_session.session_id, a_session.manifest,
        root / a_session.raw.relative_to(a_session.root),
    )  # fmt: skip
    seed_session(migrated_engine, s, status=SessionStatus.PROCESSED)
    settings = make_settings(
        data_root=root, models_root=model_b_root, pipeline_b_model=MODEL,
        face_landmarker_model=face_model,
    )  # fmt: skip
    monkeypatch.setattr(cli, "settings", lambda: settings)
    monkeypatch.setattr(cli, "session_factory", lambda: make_session_factory(migrated_engine))
    monkeypatch.setattr(cli, "open_session", lambda: make_session_factory(migrated_engine)())
    monkeypatch.setattr(cli, "log_level", lambda: "INFO")
    return s


def test_a_session_is_run_here_and_now(here: FixtureSession, migrated_engine: Engine) -> None:
    result = runner.invoke(cli.app, ["process-b", str(here.session_id)])
    assert result.exit_code == 0, result.output
    assert f"Session {here.session_id}: Pipeline B done" in result.output
    assert "included for B: True" in result.output and MODEL in result.output
    assert (derived_dir(here.root, here.study_id, here.session_id) / STAGES_B_FILE).exists()
    with Db(migrated_engine) as db:
        assert "pipeline_b" in (db.get_one(SessionRow, here.session_id).quality or {})


def test_a_single_stage_can_be_asked_for_and_a_bad_one_is_refused(
    here: FixtureSession,
) -> None:
    only = runner.invoke(cli.app, ["process-b", str(here.session_id), "--only", "features_b"])
    assert only.exit_code == 0, only.output
    bad = runner.invoke(cli.app, ["process-b", str(here.session_id), "--only", "plots"])
    assert bad.exit_code == 1 and "unknown stage" in bad.output


def test_without_the_weights_nothing_runs_and_the_reason_is_shown(
    here: FixtureSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "empty").mkdir()
    settings = make_settings(data_root=here.root, models_root=tmp_path / "empty",
                             pipeline_b_model=MODEL)  # fmt: skip
    monkeypatch.setattr(cli, "settings", lambda: settings)
    result = runner.invoke(cli.app, ["process-b", str(here.session_id)])
    assert result.exit_code == 1 and "scripts/fetch_models.sh" in result.output


def test_a_study_gets_one_queued_job_per_processed_session(
    here: FixtureSession, migrated_engine: Engine
) -> None:
    result = runner.invoke(cli.app, ["process-b", "--study", str(here.study_id)])
    assert result.exit_code == 0, result.output
    assert f"Queued Pipeline B for 1 session(s) of study {here.study_id}" in result.output
    with Db(migrated_engine) as db:
        (job,) = db.scalars(select(Job)).all()
        assert job.kind is JobKind.PROCESS_PIPELINE_B and job.status is JobStatus.QUEUED
        assert job.args == {"session_id": str(here.session_id)}
    again = runner.invoke(cli.app, ["process-b", "--study", str(here.study_id)])
    assert "Queued Pipeline B for 0 session(s)" in again.output  # already queued
    assert "already queued" in again.output


def test_the_arguments_are_checked(here: FixtureSession) -> None:
    both = runner.invoke(cli.app, ["process-b", str(here.session_id), "--study", "1"])
    neither = runner.invoke(cli.app, ["process-b"])
    assert both.exit_code == neither.exit_code == 1
    assert "not both or neither" in both.output and "not both or neither" in neither.output
    missing = runner.invoke(cli.app, ["process-b", "00000000-0000-0000-0000-000000000001"])
    assert missing.exit_code == 1 and "does not exist" in missing.output
