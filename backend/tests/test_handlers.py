"""The `process_session` job on real PostgreSQL and the recorded session (PLAN §6, §8)."""

import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db

from app.db import make_session_factory
from app.jobs import enqueue
from app.models import (
    Job,
    JobKind,
    JobStatus,
    SessionStatus,
)
from app.models import Session as SessionRow
from tests.dbutil import make_settings
from tests.session_db import seed_session
from tests.session_fixture import FixtureSession, copy_fixture_session
from worker.handlers import make_process_session
from worker.pipeline import runner
from worker.run import process_one


@pytest.fixture
def seeded(migrated_engine: Engine, tmp_path: Path) -> FixtureSession:
    """The recorded session on disk and in the database, status `uploaded`."""
    s = copy_fixture_session(tmp_path / "data")
    seed_session(
        migrated_engine,
        s,
        flags=["calibration_gate_failed", "sync_weak_peak", "calibration_failed"],
    )
    return s


def run_job(engine: Engine, s: FixtureSession, model: Path, **args: Any) -> Job:
    settings = make_settings(data_root=s.root, face_landmarker_model=model)
    factory = make_session_factory(engine)
    with factory() as db:
        job = enqueue(db, JobKind.PROCESS_SESSION, {"session_id": str(s.session_id), **args})
        db.commit()
        job_id = job.id
    handler = make_process_session(factory, settings)
    assert process_one(factory, {JobKind.PROCESS_SESSION: handler}) is True
    with factory() as db:
        return db.get_one(Job, job_id)


def session_row(engine: Engine, s: FixtureSession) -> SessionRow:
    with Db(engine) as db:
        return db.get_one(SessionRow, s.session_id)


def test_processing_a_session_end_to_end(
    migrated_engine: Engine,
    seeded: FixtureSession,
    face_model: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[SessionStatus] = []
    real = runner.run_pipeline

    def watching(*args: Any, **kwargs: Any) -> Any:
        seen.append(session_row(migrated_engine, seeded).status)
        return real(*args, **kwargs)

    monkeypatch.setattr(runner, "run_pipeline", watching)

    job = run_job(migrated_engine, seeded, face_model)

    assert job.status is JobStatus.DONE and job.log.startswith("processed: ingest, landmarks")
    assert seen == [SessionStatus.PROCESSING]  # while the stages ran
    row = session_row(migrated_engine, seeded)
    assert row.status is SessionStatus.PROCESSED
    assert row.quality is not None and row.quality["inclusion"]["included"] is False
    # (JSONB does not keep the order of keys; the file `quality.json` does.)
    assert set(row.quality["stages"]) == {
        "ingest", "landmarks", "features", "sync", "calibration", "gaze", "quality",
    }  # fmt: skip
    # Flags of the pipeline are recomputed: the browser's stays, the stale ones go, the new come.
    assert row.flags == ["calibration_gate_failed", "sync_short"]


def test_a_stage_that_fails_fails_the_job_and_the_session(
    migrated_engine: Engine, seeded: FixtureSession, face_model: Path
) -> None:
    (seeded.raw / "chunks" / "webcam" / "000002.bin").unlink()
    job = run_job(migrated_engine, seeded, face_model)
    assert job.status is JobStatus.FAILED
    assert "stage 'ingest' failed" in job.log and "webcam chunk 2" in job.log
    row = session_row(migrated_engine, seeded)
    assert row.status is SessionStatus.FAILED and row.quality is None


def test_a_session_can_be_processed_again_from_a_stage(
    migrated_engine: Engine, seeded: FixtureSession, face_model: Path
) -> None:
    assert run_job(migrated_engine, seeded, face_model).status is JobStatus.DONE
    first = session_row(migrated_engine, seeded).quality
    job = run_job(migrated_engine, seeded, face_model, from_stage="calibration")
    assert job.status is JobStatus.DONE
    row = session_row(migrated_engine, seeded)
    assert row.status is SessionStatus.PROCESSED and row.quality is not None and first is not None
    assert row.quality["stages"]["ingest"] == first["stages"]["ingest"]  # kept from the first run


def test_a_failed_session_is_processed_again_and_recovers(
    migrated_engine: Engine, seeded: FixtureSession, face_model: Path
) -> None:
    chunk = seeded.raw / "chunks" / "webcam" / "000002.bin"
    saved = chunk.read_bytes()
    chunk.unlink()
    assert run_job(migrated_engine, seeded, face_model).status is JobStatus.FAILED
    chunk.write_bytes(saved)
    assert run_job(migrated_engine, seeded, face_model).status is JobStatus.DONE
    assert session_row(migrated_engine, seeded).status is SessionStatus.PROCESSED


def test_an_early_stage_alone_on_an_unprocessed_session_leaves_it_uploaded(
    migrated_engine: Engine, seeded: FixtureSession, face_model: Path
) -> None:
    job = run_job(migrated_engine, seeded, face_model, only="ingest")
    assert job.status is JobStatus.DONE
    row = session_row(migrated_engine, seeded)
    assert row.status is SessionStatus.UPLOADED and row.quality is None  # no report yet


@pytest.mark.parametrize(
    ("status", "message"),
    [(SessionStatus.RECORDING, "not complete"), (SessionStatus.WITHDRAWN, "not complete")],
)
def test_a_session_that_is_not_complete_is_refused(
    migrated_engine: Engine,
    seeded: FixtureSession,
    face_model: Path,
    status: SessionStatus,
    message: str,
) -> None:
    with Db(migrated_engine) as db:
        db.get_one(SessionRow, seeded.session_id).status = status
        db.commit()
    job = run_job(migrated_engine, seeded, face_model)
    assert job.status is JobStatus.FAILED and message in job.log
    assert session_row(migrated_engine, seeded).status is status  # untouched


def test_unknown_session_and_missing_args_fail_the_job(
    migrated_engine: Engine, seeded: FixtureSession, face_model: Path
) -> None:
    factory = make_session_factory(migrated_engine)
    handler = make_process_session(factory, make_settings(data_root=seeded.root))
    with factory() as db:
        enqueue(db, JobKind.PROCESS_SESSION, {"session_id": str(uuid.UUID(int=99))})
        enqueue(db, JobKind.PROCESS_SESSION, {})
        db.commit()
    assert process_one(factory, {JobKind.PROCESS_SESSION: handler}) is True
    assert process_one(factory, {JobKind.PROCESS_SESSION: handler}) is True
    with Db(migrated_engine) as db:
        logs = [j.log for j in db.query(Job).order_by(Job.id)]
    assert "does not exist" in logs[0] and "needs a session_id" in logs[1]
