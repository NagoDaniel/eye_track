"""The session commands of the CLI: process, export-session, import-session (PLAN §12.3)."""

import logging
import shutil
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db
from typer.testing import CliRunner

import cli
from app.db import make_session_factory
from app.jobs import enqueue
from app.models import Job, JobKind, JobStatus, SessionStatus
from app.models import Session as SessionRow
from tests.dbutil import make_settings
from tests.session_db import seed_session
from tests.session_fixture import FixtureSession, copy_fixture_session

runner = CliRunner()


@pytest.fixture(autouse=True)
def no_leftover_log_handlers() -> Iterator[None]:
    """The CLI points logging at the (captured) stderr of the test; take that away again."""
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def here(
    migrated_engine: Engine, tmp_path: Path, face_model: Path, monkeypatch: pytest.MonkeyPatch
) -> FixtureSession:
    """The recorded session, uploaded, with the CLI pointed at the test database and files."""
    s = copy_fixture_session(tmp_path / "data")
    seed_session(migrated_engine, s)
    settings = make_settings(data_root=s.root, face_landmarker_model=face_model)
    monkeypatch.setattr(cli, "settings", lambda: settings)
    monkeypatch.setattr(cli, "session_factory", lambda: make_session_factory(migrated_engine))
    monkeypatch.setattr(cli, "open_session", lambda: make_session_factory(migrated_engine)())
    monkeypatch.setattr(cli, "log_level", lambda: "INFO")
    return s


def derived(s: FixtureSession) -> Path:
    return s.raw.parent / "derived"


def test_process_runs_the_pipeline_and_prints_the_stages(
    here: FixtureSession, migrated_engine: Engine
) -> None:
    result = runner.invoke(cli.app, ["process", str(here.session_id)])
    assert result.exit_code == 0, result.output
    assert f"Session {here.session_id}: processed" in result.output
    for stage in (
        "ingest",
        "landmarks",
        "features",
        "sync",
        "calibration",
        "gaze",
        "quality",
        "plots",
    ):
        assert stage in result.output
    assert "included in the analysis: False" in result.output
    assert (derived(here) / "quality.json").exists()
    with Db(migrated_engine) as db:
        assert db.get_one(SessionRow, here.session_id).status is SessionStatus.PROCESSED


def test_process_from_a_stage_and_one_stage_only(here: FixtureSession) -> None:
    assert runner.invoke(cli.app, ["process", str(here.session_id)]).exit_code == 0
    again = runner.invoke(cli.app, ["process", str(here.session_id), "--from-stage", "calibration"])
    assert again.exit_code == 0, again.output
    only = runner.invoke(cli.app, ["process", str(here.session_id), "--only", "features"])
    assert only.exit_code == 0, only.output
    assert "features" in only.output


def test_process_keeps_the_intermediate_file_only_when_asked(here: FixtureSession) -> None:
    kept = derived(here) / "intermediates" / "webcam_concat.webm"
    assert (
        runner.invoke(cli.app, ["process", str(here.session_id), "--only", "ingest"]).exit_code == 0
    )
    assert not kept.exists()
    args = ["process", str(here.session_id), "--only", "ingest", "--keep-intermediates"]
    assert runner.invoke(cli.app, args).exit_code == 0
    assert kept.is_file() and kept.stat().st_size > 1000
    assert (
        runner.invoke(cli.app, ["process", str(here.session_id), "--only", "ingest"]).exit_code == 0
    )
    assert not kept.exists()  # a run without the flag does not leave a stale one


def test_process_refuses_bad_arguments(here: FixtureSession) -> None:
    sid = str(here.session_id)
    for args, message in (
        (["process", "nope"], "not a session id"),
        (["process", sid, "--from-stage", "bogus"], "unknown stage 'bogus'"),
        (["process", sid, "--only", "bogus"], "unknown stage 'bogus'"),
        (["process", sid, "--from-stage", "sync", "--only", "sync"], "not both"),
        (["process", "00000000-0000-0000-0000-0000000000aa"], "does not exist"),
    ):
        result = runner.invoke(cli.app, args)
        assert result.exit_code == 1 and message in result.output, (args, result.output)


def test_process_refuses_a_session_that_is_busy(
    here: FixtureSession, migrated_engine: Engine
) -> None:
    with Db(migrated_engine) as db:
        job_id = enqueue(db, JobKind.PROCESS_SESSION, {"session_id": str(here.session_id)}).id
        db.commit()
    queued = runner.invoke(cli.app, ["process", str(here.session_id)])
    assert queued.exit_code == 1 and "already queued or running" in queued.output
    with Db(migrated_engine) as db:
        db.get_one(Job, job_id).status = JobStatus.DONE
        db.get_one(SessionRow, here.session_id).status = SessionStatus.PROCESSING
        db.commit()
    busy = runner.invoke(cli.app, ["process", str(here.session_id)])
    assert busy.exit_code == 1 and "the session is processing" in busy.output
    assert not derived(here).exists() or not (derived(here) / "quality.json").exists()


def test_process_reports_the_stage_that_failed(
    here: FixtureSession, migrated_engine: Engine
) -> None:
    (here.raw / "chunks" / "webcam" / "000001.bin").unlink()
    result = runner.invoke(cli.app, ["process", str(here.session_id)])
    assert result.exit_code == 1 and "stage 'ingest' failed" in result.output
    with Db(migrated_engine) as db:
        assert db.get_one(SessionRow, here.session_id).status is SessionStatus.FAILED


def test_export_and_import_through_the_commands(
    here: FixtureSession, migrated_engine: Engine, tmp_path: Path
) -> None:
    assert runner.invoke(cli.app, ["process", str(here.session_id)]).exit_code == 0
    archive = tmp_path / "out.zip"
    exported = runner.invoke(
        cli.app, ["export-session", str(here.session_id), "--out", str(archive)]
    )
    assert exported.exit_code == 0, exported.output
    assert "webcam video (personal data)" in exported.output
    assert "raw/webcam.webm" in zipfile.ZipFile(archive).namelist()

    clean = tmp_path / "clean.zip"
    result = runner.invoke(cli.app, ["export-session", str(here.session_id), "--out", str(clean),
                                     "--no-raw-video"])  # fmt: skip
    assert result.exit_code == 0 and "personal data" not in result.output

    # Importing the same session again is refused, with --force it replaces the local one.
    refused = runner.invoke(cli.app, ["import-session", str(archive)])
    assert (
        refused.exit_code == 1
        and "already exists" in refused.output
        and "--force" in refused.output
    )
    forced = runner.invoke(cli.app, ["import-session", str(archive), "--force"])
    assert forced.exit_code == 0, forced.output
    assert f"python cli.py process {here.session_id}" in forced.output
    with Db(migrated_engine) as db:
        assert db.get_one(SessionRow, here.session_id).status is SessionStatus.PROCESSED


def test_export_and_import_report_problems(here: FixtureSession, tmp_path: Path) -> None:
    unknown = runner.invoke(cli.app, ["export-session", "00000000-0000-0000-0000-0000000000aa"])
    assert unknown.exit_code == 1 and "does not exist" in unknown.output
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    result = runner.invoke(cli.app, ["import-session", str(broken)])
    assert result.exit_code == 1 and "not a readable ZIP" in result.output
    shutil.rmtree(here.raw.parent)
    gone = runner.invoke(cli.app, ["export-session", str(here.session_id)])
    assert gone.exit_code == 1 and "no data on disk" in gone.output
