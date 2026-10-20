"""The `process_pipeline_b` job on real PostgreSQL, the recorded session and the real small gaze
network (PLAN §8.5; DECISIONS 2026-10-20)."""

import json
import shutil
import uuid
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db

from app.db import make_session_factory
from app.jobs import claim_next, enqueue, recover_stale_running
from app.models import Job, JobKind, JobStatus, SessionStatus
from app.models import Session as SessionRow
from app.pipeline_b import (
    B_STAGE_NAMES,
    FEATURES_B_FILE,
    QUALITY_B_FILE,
    STAGES_B_FILE,
    STALE_FILE,
    queue_pipeline_b,
)
from app.service_errors import ConflictError, InvalidError, NotFoundError
from app.settings import Settings
from app.storage import derived_dir
from tests.dbutil import make_settings
from tests.session_db import seed_session
from tests.session_fixture import FixtureSession
from worker.handlers import make_process_pipeline_b, make_process_session
from worker.run import process_one

MODEL = "l2cs_mobileone_s0"


def copy_of(s: FixtureSession, tmp_path: Path) -> FixtureSession:
    root = tmp_path / "data"
    shutil.copytree(s.root, root)
    return FixtureSession(
        root, s.study_id, s.session_id, s.manifest, root / s.raw.relative_to(s.root)
    )


def settings_for(s: FixtureSession, models_root: Path, face_model: Path) -> Settings:
    return make_settings(
        data_root=s.root, models_root=models_root, pipeline_b_model=MODEL,
        face_landmarker_model=face_model,
    )  # fmt: skip


def seed_processed(engine: Engine, s: FixtureSession) -> None:
    """The session in the database as Pipeline A leaves it: `processed`, with its report."""
    seed_session(engine, s, status=SessionStatus.PROCESSED)
    report = json.loads(
        (derived_dir(s.root, s.study_id, s.session_id) / "quality.json").read_text()
    )
    with Db(engine) as db:
        db.get_one(SessionRow, s.session_id).quality = report
        db.commit()


def run_b(engine: Engine, s: FixtureSession, settings: Settings, **args: object) -> Job:
    factory = make_session_factory(engine)
    with factory() as db:
        job = enqueue(db, JobKind.PROCESS_PIPELINE_B, {"session_id": str(s.session_id), **args})
        db.commit()
        job_id = job.id
    handler = make_process_pipeline_b(factory, settings)
    assert process_one(factory, {JobKind.PROCESS_PIPELINE_B: handler}) is True
    with factory() as db:
        return db.get_one(Job, job_id)


@pytest.fixture
def a_only(migrated_engine: Engine, a_session: FixtureSession, tmp_path: Path) -> FixtureSession:
    s = copy_of(a_session, tmp_path)
    seed_processed(migrated_engine, s)
    return s


def a_files(s: FixtureSession) -> dict[str, bytes]:
    derived = derived_dir(s.root, s.study_id, s.session_id)
    return {p.name: p.read_bytes() for p in derived.iterdir() if p.is_file()}


def test_the_job_runs_b_and_leaves_a_and_the_session_status_alone(
    migrated_engine: Engine, a_only: FixtureSession, model_b_root: Path, face_model: Path
) -> None:
    before = a_files(a_only)
    job = run_b(migrated_engine, a_only, settings_for(a_only, model_b_root, face_model))
    assert job.status is JobStatus.DONE, job.log
    derived = derived_dir(a_only.root, a_only.study_id, a_only.session_id)
    after = a_files(a_only)
    for name, data in before.items():
        assert after[name] == data, name  # A's files, quality.json and stages.json are untouched
    new = set(after) - set(before)
    assert new == {
        FEATURES_B_FILE, "calibration_b.json", "gaze_b.parquet", "fixations_b.parquet",
        QUALITY_B_FILE, STAGES_B_FILE,
    }  # fmt: skip
    stages = json.loads((derived / STAGES_B_FILE).read_text())["stages"]
    assert list(stages) == list(B_STAGE_NAMES)
    assert all(e["status"] == "ok" for e in stages.values()), stages
    assert stages["features_b"]["summary"]["model"] == MODEL
    with Db(migrated_engine) as db:
        row = db.get_one(SessionRow, a_only.session_id)
        assert row.status is SessionStatus.PROCESSED
        line = (row.quality or {})["pipeline_b"]
        assert line["model"] == MODEL and line["stale"] is False
        assert line["included"] is True and line["calibration_status"] == "ok"
        assert (row.quality or {})["inclusion"]  # A's report is still there


def test_without_the_weights_the_job_fails_clearly_and_a_is_untouched(
    migrated_engine: Engine, a_only: FixtureSession, tmp_path: Path, face_model: Path
) -> None:
    before = a_files(a_only)
    empty = tmp_path / "no-models"
    empty.mkdir()
    job = run_b(migrated_engine, a_only, settings_for(a_only, empty, face_model))
    assert job.status is JobStatus.FAILED
    assert "scripts/fetch_models.sh" in job.log and MODEL in job.log
    after = a_files(a_only)
    assert {n: after[n] for n in before} == before
    stages = json.loads(
        (derived_dir(a_only.root, a_only.study_id, a_only.session_id) / STAGES_B_FILE).read_text()
    )["stages"]
    assert (
        stages["features_b"]["status"] == "failed"
        and "fetch_models" in stages["features_b"]["error"]
    )
    with Db(migrated_engine) as db:
        row = db.get_one(SessionRow, a_only.session_id)
        assert row.status is SessionStatus.PROCESSED and "pipeline_b" not in (row.quality or {})


def test_a_session_that_a_has_not_processed_is_refused(
    migrated_engine: Engine, a_session: FixtureSession, tmp_path: Path, model_b_root: Path,
    face_model: Path,
) -> None:  # fmt: skip
    s = copy_of(a_session, tmp_path)
    seed_session(migrated_engine, s, status=SessionStatus.UPLOADED)
    job = run_b(migrated_engine, s, settings_for(s, model_b_root, face_model))
    assert job.status is JobStatus.FAILED and "Pipeline A must have processed it" in job.log


def test_when_a_remakes_b_s_inputs_b_is_stale_until_it_runs_again(
    migrated_engine: Engine, a_only: FixtureSession, model_b_root: Path, face_model: Path
) -> None:
    settings = settings_for(a_only, model_b_root, face_model)
    assert run_b(migrated_engine, a_only, settings).status is JobStatus.DONE
    derived = derived_dir(a_only.root, a_only.study_id, a_only.session_id)
    factory = make_session_factory(migrated_engine)
    process_a = make_process_session(factory, settings)

    def rerun_a(from_stage: str) -> None:
        with factory() as db:
            enqueue(db, JobKind.PROCESS_SESSION,
                    {"session_id": str(a_only.session_id), "from_stage": from_stage})  # fmt: skip
            db.commit()
        assert process_one(factory, {JobKind.PROCESS_SESSION: process_a}) is True

    def b_line() -> dict[str, object]:
        with Db(migrated_engine) as db:
            return dict((db.get_one(SessionRow, a_only.session_id).quality or {})["pipeline_b"])

    rerun_a("calibration")  # A's calibration and gaze do not feed B
    assert not (derived / STALE_FILE).exists() and b_line()["stale"] is False

    rerun_a("sync")  # the sync does
    assert json.loads((derived / STALE_FILE).read_text())["by_stage"] == "sync"
    assert b_line()["stale"] is True and b_line()["model"] == MODEL  # B's line survives, marked

    refused = run_b(migrated_engine, a_only, settings, only="quality_b")
    assert refused.status is JobStatus.FAILED and "stale" in refused.log
    assert (derived / STALE_FILE).exists()

    fresh = run_b(migrated_engine, a_only, settings, from_stage="features_b")
    assert fresh.status is JobStatus.DONE, fresh.log
    assert not (derived / STALE_FILE).exists() and b_line()["stale"] is False


def test_a_that_never_ran_b_is_not_marked(
    migrated_engine: Engine, a_only: FixtureSession, model_b_root: Path, face_model: Path
) -> None:
    factory = make_session_factory(migrated_engine)
    settings = settings_for(a_only, model_b_root, face_model)
    with factory() as db:
        enqueue(db, JobKind.PROCESS_SESSION,
                {"session_id": str(a_only.session_id), "from_stage": "sync"})  # fmt: skip
        db.commit()
    assert process_one(factory, {JobKind.PROCESS_SESSION: make_process_session(factory, settings)})
    derived = derived_dir(a_only.root, a_only.study_id, a_only.session_id)
    assert not (derived / STALE_FILE).exists()
    with Db(migrated_engine) as db:
        assert "pipeline_b" not in (db.get_one(SessionRow, a_only.session_id).quality or {})


def test_every_other_job_goes_before_a_pipeline_b_job(migrated_engine: Engine) -> None:
    with Db(migrated_engine) as db:
        b = enqueue(db, JobKind.PROCESS_PIPELINE_B, {"session_id": "1"}).id
        a1 = enqueue(db, JobKind.PROCESS_SESSION, {"session_id": "2"}).id
        b2 = enqueue(db, JobKind.PROCESS_PIPELINE_B, {"session_id": "3"}).id
        replay = enqueue(db, JobKind.RENDER_DEBUG, {"session_id": "4"}).id
        db.commit()
        order = []
        while (job := claim_next(db)) is not None:
            order.append(job.id)
            job.status = JobStatus.DONE
            db.commit()
    assert order == [a1, replay, b, b2]  # B jobs last, each group in queue order


def test_a_pipeline_b_job_that_was_running_when_the_worker_died_is_requeued(
    migrated_engine: Engine, a_only: FixtureSession
) -> None:
    with Db(migrated_engine) as db:
        job = enqueue(db, JobKind.PROCESS_PIPELINE_B, {"session_id": str(a_only.session_id)})
        job.status, job.attempts = JobStatus.RUNNING, 1
        db.commit()
        result = recover_stale_running(db, max_attempts=3)
        db.commit()
        assert result.requeued == [job.id]
        assert db.get_one(Job, job.id).status is JobStatus.QUEUED
        assert db.get_one(SessionRow, a_only.session_id).status is SessionStatus.PROCESSED


def test_queueing_checks_the_session_the_stage_and_the_weights(
    migrated_engine: Engine, a_only: FixtureSession, model_b_root: Path, tmp_path: Path
) -> None:
    root = a_only.root
    weights = (model_b_root, MODEL)
    with Db(migrated_engine) as db:
        with pytest.raises(NotFoundError):
            queue_pipeline_b(db, uuid.uuid4(), root)
        with pytest.raises(InvalidError, match="unknown stage"):
            queue_pipeline_b(db, a_only.session_id, root, "plots")
        (tmp_path / "empty").mkdir()
        with pytest.raises(ConflictError, match="fetch_models.sh"):
            queue_pipeline_b(db, a_only.session_id, root, weights=(tmp_path / "empty", MODEL))
        job = queue_pipeline_b(db, a_only.session_id, root, "features_b", weights)
        db.commit()
        assert job.args == {"session_id": str(a_only.session_id), "from_stage": "features_b"}
        with pytest.raises(ConflictError, match="already queued"):
            queue_pipeline_b(db, a_only.session_id, root)
        row = db.get_one(SessionRow, a_only.session_id)
        row.status = SessionStatus.FAILED
        job.status = JobStatus.DONE
        db.commit()
        with pytest.raises(ConflictError, match="Pipeline A must have processed"):
            queue_pipeline_b(db, a_only.session_id, root)
        row.status = SessionStatus.PROCESSED
        (derived_dir(root, a_only.study_id, a_only.session_id) / "sync.json").unlink()
        with pytest.raises(ConflictError, match="sync.json"):
            queue_pipeline_b(db, a_only.session_id, root)
