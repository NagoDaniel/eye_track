"""Stopping jobs (DECISIONS 2026-10-22): the flag, the worker's check, and the jobs that notice."""

import shutil
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db

import worker.cancel as cancel
from app.db import make_session_factory
from app.jobs import CANCELLED_LOG, enqueue, recover_stale_running, request_cancel
from app.models import Job, JobKind, JobStatus, SessionStatus
from app.models import Session as SessionRow
from app.pipeline_b import FEATURES_B_FILE
from app.storage import derived_dir, webcam_path
from tests.dbutil import make_settings
from tests.session_db import seed_session
from tests.session_fixture import FixtureSession, copy_fixture_session
from worker.cancel import JobCancelled, check_cancelled, job_context
from worker.handlers import make_process_session
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURES_FILE
from worker.pipeline.features_b import compute_features_b
from worker.pipeline.landmarks import LANDMARKS_FILE
from worker.run import process_one


@pytest.fixture(autouse=True)
def poll_every_time(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cancel, "POLL_EVERY_S", 0.0)


def make_job(db: Db, status: JobStatus, kind: JobKind = JobKind.PROCESS_PIPELINE_B) -> Job:
    job = enqueue(db, kind, {"session_id": "x"})
    job.status = status
    db.flush()
    return job


def test_a_queued_job_is_cancelled_at_once_and_a_running_one_is_asked(
    migrated_engine: Engine,
) -> None:
    with Db(migrated_engine) as db:
        queued = make_job(db, JobStatus.QUEUED)
        running = make_job(db, JobStatus.RUNNING)
        done = make_job(db, JobStatus.DONE)
        failed = make_job(db, JobStatus.FAILED)
        assert request_cancel(db, queued) == "cancelled"
        assert queued.status is JobStatus.FAILED and queued.log == CANCELLED_LOG
        assert queued.finished_at is not None and queued.cancel_requested is False
        assert request_cancel(db, running) == "cancelling"
        assert running.status is JobStatus.RUNNING and running.cancel_requested is True
        for finished in (done, failed):
            with pytest.raises(ValueError, match="nothing to cancel"):
                request_cancel(db, finished)


def test_the_check_does_nothing_outside_a_job_and_raises_inside_one_that_was_cancelled(
    migrated_engine: Engine,
) -> None:
    factory = make_session_factory(migrated_engine)
    check_cancelled(force=True)  # no job: nothing happens
    with factory() as db:
        job = make_job(db, JobStatus.RUNNING)
        db.commit()
        job_id = job.id
    with job_context(factory, job_id):
        check_cancelled()  # not asked yet
        with factory() as db:
            db.get_one(Job, job_id).cancel_requested = True
            db.commit()
        with pytest.raises(JobCancelled, match="cancelled by the admin"):
            check_cancelled()
    check_cancelled(force=True)  # and outside the job again: nothing


def test_the_database_is_asked_at_most_once_per_interval(
    migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cancel, "POLL_EVERY_S", 3600.0)
    factory = make_session_factory(migrated_engine)
    with factory() as db:
        job = make_job(db, JobStatus.RUNNING)
        db.commit()
        job_id = job.id
    with job_context(factory, job_id):
        check_cancelled()  # the first look
        with factory() as db:
            db.get_one(Job, job_id).cancel_requested = True
            db.commit()
        check_cancelled()  # too soon to look again
        with pytest.raises(JobCancelled):
            check_cancelled(force=True)


def test_a_job_that_notices_the_request_ends_failed_with_the_cancel_log(
    migrated_engine: Engine,
) -> None:
    factory = make_session_factory(migrated_engine)
    with factory() as db:
        job = enqueue(db, JobKind.ANALYZE_VIDEO, {})
        db.commit()
        job_id = job.id

    def handler(job: Job) -> str:
        for step in range(10):
            if step == 3:  # the admin presses the button while the job runs
                with factory() as db:
                    db.get_one(Job, job.id).cancel_requested = True
                    db.commit()
            check_cancelled()
        return "finished"

    assert process_one(factory, {JobKind.ANALYZE_VIDEO: handler}) is True
    with factory() as db:
        done = db.get_one(Job, job_id)
        assert done.status is JobStatus.FAILED and done.log == CANCELLED_LOG


def test_a_cancelled_job_that_was_running_when_the_worker_died_is_not_run_again(
    migrated_engine: Engine,
) -> None:
    with Db(migrated_engine) as db:
        job = make_job(db, JobStatus.RUNNING)
        job.attempts, job.cancel_requested = 1, True
        other = make_job(db, JobStatus.RUNNING)
        other.attempts = 1
        db.flush()
        result = recover_stale_running(db, max_attempts=3)
        assert result.failed == [job.id] and result.requeued == [other.id]
        assert job.status is JobStatus.FAILED and job.log == CANCELLED_LOG


def test_a_processing_job_stopped_before_its_first_stage_leaves_the_session_as_it_was(
    migrated_engine: Engine, tmp_path: Path, face_model: Path
) -> None:
    s = copy_fixture_session(tmp_path / "data")
    seed_session(migrated_engine, s, status=SessionStatus.UPLOADED)
    factory = make_session_factory(migrated_engine)
    with factory() as db:
        job = enqueue(db, JobKind.PROCESS_SESSION, {"session_id": str(s.session_id)})
        job.cancel_requested = True  # asked before the worker started on it
        db.commit()
        job_id = job.id
    settings = make_settings(data_root=s.root, face_landmarker_model=face_model)
    handler = make_process_session(factory, settings)
    assert process_one(factory, {JobKind.PROCESS_SESSION: handler}) is True
    with factory() as db:
        assert db.get_one(Job, job_id).log == CANCELLED_LOG
        assert db.get_one(SessionRow, s.session_id).status is SessionStatus.UPLOADED
    assert not (derived_dir(s.root, s.study_id, s.session_id) / FEATURES_FILE).exists()


class CancelAfter:
    """A stand-in for the gaze network that asks for the job to stop after `batches` batches."""

    def __init__(self, factory, job_id: int, batches: int) -> None:  # type: ignore[no-untyped-def]
        self.factory, self.job_id, self.batches, self.calls = factory, job_id, batches, 0

    def predict(self, images, batch_size: int = 8):  # type: ignore[no-untyped-def]
        import numpy as np

        self.calls += 1
        if self.calls == self.batches:
            with self.factory() as db:
                db.get_one(Job, self.job_id).cancel_requested = True
                db.commit()
        n = len(images)
        return np.zeros(n), np.zeros(n)


def test_the_gaze_network_loop_stops_when_asked_and_writes_no_file(
    migrated_engine: Engine, a_session: FixtureSession, tmp_path: Path
) -> None:
    root = tmp_path / "data"
    shutil.copytree(a_session.root, root)
    derived = derived_dir(root, a_session.study_id, a_session.session_id)
    factory = make_session_factory(migrated_engine)
    with factory() as db:
        job = enqueue(db, JobKind.PROCESS_PIPELINE_B, {})
        db.commit()
        job_id = job.id
    estimator = CancelAfter(factory, job_id, batches=3)

    def handler(job: Job) -> str:
        compute_features_b(
            webcam_path(root, a_session.study_id, a_session.session_id),
            derived / LANDMARKS_FILE, derived / FEATURES_FILE, derived / FEATURES_B_FILE,
            estimator, {}, PipelineConfig(),
        )  # fmt: skip
        return "finished"

    assert process_one(factory, {JobKind.PROCESS_PIPELINE_B: handler}) is True
    with factory() as db:
        done = db.get_one(Job, job_id)
        assert done.status is JobStatus.FAILED and done.log == CANCELLED_LOG
    assert estimator.calls <= 4  # it stopped at the next check, not at the end of 340 frames
    assert not (derived / FEATURES_B_FILE).exists()
