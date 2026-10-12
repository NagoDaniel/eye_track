import json
import logging
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from app.db import make_session_factory
from app.jobs import enqueue
from app.models import Job, JobKind, JobStatus, WorkerHeartbeat
from worker.healthcheck import heartbeat_is_fresh
from worker.run import Heartbeat, beat, process_one


def factory(engine: Engine) -> sessionmaker[DbSession]:
    return make_session_factory(engine)


def test_process_one_runs_handler_and_marks_done(migrated_engine: Engine) -> None:
    f = factory(migrated_engine)
    seen: list[dict[str, object]] = []
    with f() as db:
        enqueue(db, JobKind.PROCESS_SESSION, {"session_id": "s1"})
        db.commit()

    def handler(job: Job) -> str:
        seen.append(job.args)
        return "processed"

    assert process_one(f, {JobKind.PROCESS_SESSION: handler}) is True
    assert seen == [{"session_id": "s1"}]
    with f() as db:
        job = db.get(Job, 1)
        assert job is not None and job.status is JobStatus.DONE and job.log == "processed"


def test_process_one_returns_false_when_idle(migrated_engine: Engine) -> None:
    assert process_one(factory(migrated_engine), {}) is False


def test_handler_exception_fails_job_with_traceback(migrated_engine: Engine) -> None:
    f = factory(migrated_engine)
    with f() as db:
        enqueue(db, JobKind.PROCESS_SESSION, {})
        db.commit()

    def handler(job: Job) -> str:
        raise RuntimeError("pipeline exploded")

    assert process_one(f, {JobKind.PROCESS_SESSION: handler}) is True
    with f() as db:
        job = db.get(Job, 1)
        assert job is not None and job.status is JobStatus.FAILED
        assert "RuntimeError: pipeline exploded" in job.log


def test_job_kind_without_handler_fails_loudly(migrated_engine: Engine) -> None:
    f = factory(migrated_engine)
    with f() as db:
        enqueue(db, JobKind.COMPARE_PIPELINES, {})
        db.commit()
    assert process_one(f, {}) is True
    with f() as db:
        job = db.get(Job, 1)
        assert job is not None and job.status is JobStatus.FAILED
        assert "no handler" in job.log and "compare_pipelines" in job.log


def test_process_one_logs_job_id_and_stage(
    migrated_engine: Engine, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.logging import configure_logging

    configure_logging("INFO")
    f = factory(migrated_engine)
    with f() as db:
        enqueue(db, JobKind.PROCESS_SESSION, {})
        db.commit()
    process_one(f, {JobKind.PROCESS_SESSION: lambda job: "ok"})
    records = [json.loads(x) for x in capsys.readouterr().err.splitlines()]
    assert any(r.get("job_id") == "1" for r in records)
    logging.getLogger().handlers.clear()


def test_beat_upserts_single_row(migrated_engine: Engine) -> None:
    f = factory(migrated_engine)
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = t1 + timedelta(seconds=30)
    with f() as db:
        beat(db, "w1", t1)
        db.commit()
        beat(db, "w1", t2)
        db.commit()
        rows = db.query(WorkerHeartbeat).all()
        assert len(rows) == 1 and rows[0].beat_at == t2


def test_heartbeat_thread_updates_while_running(migrated_engine: Engine) -> None:
    f = factory(migrated_engine)
    hb = Heartbeat(f, "w1", interval_s=0.05)
    hb.start()
    try:
        time.sleep(0.4)
        with f() as db:
            first = db.get(WorkerHeartbeat, "w1")
            assert first is not None
            t_first = first.beat_at
        time.sleep(0.2)
        with f() as db:
            later = db.get(WorkerHeartbeat, "w1")
            assert later is not None and later.beat_at > t_first
    finally:
        hb.stop()


def test_healthcheck_fresh_stale_and_missing(migrated_engine: Engine) -> None:
    f = factory(migrated_engine)
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    with f() as db:
        assert heartbeat_is_fresh(db, max_age_s=90, now=now) is False  # no worker yet
        beat(db, "w1", now - timedelta(seconds=45))
        db.commit()
        assert heartbeat_is_fresh(db, max_age_s=90, now=now) is True
        beat(db, "w1", now - timedelta(seconds=200))
        db.commit()
        assert heartbeat_is_fresh(db, max_age_s=90, now=now) is False
