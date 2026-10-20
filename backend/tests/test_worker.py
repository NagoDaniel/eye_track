import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from app.db import make_session_factory
from app.jobs import enqueue
from app.models import Job, JobKind, JobStatus, WorkerHeartbeat
from tests.dbutil import BACKEND
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


def test_process_one_logs_job_id(
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
    started = [r for r in records if r["msg"] == "job started"]
    assert started and started[0]["job_id"] == "1" and started[0]["kind"] == "process_session"
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
        # a loaded machine may be slow to beat again: wait for it instead of for a fixed time
        deadline = time.monotonic() + 5
        while True:
            with f() as db:
                later = db.get(WorkerHeartbeat, "w1")
                assert later is not None
                if later.beat_at > t_first or time.monotonic() > deadline:
                    break
            time.sleep(0.05)
        assert later.beat_at > t_first
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


@pytest.mark.parametrize("value", ["0", "2"])
def test_main_rejects_concurrency_other_than_one(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    from worker.run import main

    monkeypatch.setenv("SECRET_KEY", "k" * 40)
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
    monkeypatch.setenv("WORKER_CONCURRENCY", value)
    with pytest.raises(SystemExit, match="WORKER_CONCURRENCY must be 1"):
        main()
    logging.getLogger().handlers.clear()


def _worker_env(engine: Engine) -> dict[str, str]:
    url = engine.url
    assert url.host and url.port and url.username and url.password and url.database
    return {
        **os.environ,
        "SECRET_KEY": "k" * 40,
        "POSTGRES_HOST": url.host,
        "POSTGRES_PORT": str(url.port),
        "POSTGRES_USER": url.username,
        "POSTGRES_PASSWORD": url.password,
        "POSTGRES_DB": url.database,
        "WORKER_CONCURRENCY": "1",
        "WORKER_POLL_INTERVAL_S": "0.1",
        "JOB_MAX_ATTEMPTS": "3",
        "LOG_LEVEL": "INFO",
    }


def test_main_recovers_stale_jobs_at_start_and_stops_on_sigterm(migrated_engine: Engine) -> None:
    """A worker killed mid-job leaves `running` rows (R2); the next worker deals with them."""
    f = factory(migrated_engine)
    with f() as db:
        for attempts in (1, 3):  # one more try left, and none
            job = enqueue(db, JobKind.PROCESS_SESSION, {})
            job.status = JobStatus.RUNNING
            job.attempts = attempts
        db.commit()

    proc = subprocess.Popen(
        [sys.executable, "-m", "worker.run"],
        cwd=BACKEND,
        env=_worker_env(migrated_engine),
        stderr=subprocess.PIPE,
        text=True,
    )
    problem = None
    jobs: dict[int, Job] = {}
    deadline = time.monotonic() + 30
    while problem is None:
        with f() as db:
            jobs = {j.id: j for j in db.query(Job).all()}
        if jobs and all(j.status is JobStatus.FAILED for j in jobs.values()):
            break
        if proc.poll() is not None:
            problem = f"the worker exited early with code {proc.returncode}"
        elif time.monotonic() > deadline:
            problem = "the stale jobs were not dealt with within 30 s"
        else:
            time.sleep(0.2)
    proc.send_signal(signal.SIGTERM)
    try:
        _, stderr = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, stderr = proc.communicate()
        problem = problem or "the worker did not stop within 30 s of SIGTERM"
    assert problem is None, f"{problem}\nworker log:\n{stderr}"

    # Job 1 was requeued and claimed again (attempt 2); it has no session_id, so the real
    # process_session handler refuses it. Job 2 had used all attempts and failed as "worker died".
    assert proc.returncode == 0, stderr
    assert jobs[1].attempts == 2 and "needs a session_id" in jobs[1].log
    assert jobs[2].attempts == 3 and "worker died" in jobs[2].log
    lines = [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]
    assert any(r["msg"] == "stale jobs recovered" for r in lines)
    assert lines[-1]["msg"] == "worker stopped"
