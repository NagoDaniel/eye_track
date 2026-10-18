"""DB-backed job queue (PLAN §4). Workers claim with `SELECT … FOR UPDATE SKIP LOCKED`.

Functions only flush; the caller owns the transaction and must commit.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, JobKind, JobStatus, SessionStatus
from app.models import Session as SessionRow


def enqueue(db: Session, kind: JobKind, args: dict[str, Any]) -> Job:
    job = Job(kind=kind, args=args)
    db.add(job)
    db.flush()
    return job


def claim_next(db: Session) -> Job | None:
    """Lock the oldest queued job, mark it running and count the attempt."""
    stmt = (
        select(Job)
        .where(Job.status == JobStatus.QUEUED)
        .order_by(Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = db.scalars(stmt).first()
    if job is None:
        return None
    job.status = JobStatus.RUNNING
    job.attempts += 1
    job.started_at = datetime.now(UTC)
    db.flush()
    return job


def _finish(db: Session, job: Job, status: JobStatus, log: str) -> None:
    job.status = status
    job.log = log
    job.finished_at = datetime.now(UTC)
    db.flush()


def mark_done(db: Session, job: Job, log: str = "") -> None:
    _finish(db, job, JobStatus.DONE, log)


def mark_failed(db: Session, job: Job, log: str) -> None:
    _finish(db, job, JobStatus.FAILED, log)


@dataclass(frozen=True)
class Recovery:
    requeued: list[int]
    failed: list[int]


def _session_of(db: Session, job: Job) -> SessionRow | None:
    raw = job.args.get("session_id") if job.kind is JobKind.PROCESS_SESSION else None
    try:
        return db.get(SessionRow, uuid.UUID(str(raw)))
    except ValueError:
        return None


def recover_stale_running(db: Session, max_attempts: int) -> Recovery:
    """Deal with jobs left `running` by a worker that died (for example by an image update).

    Call only when no other worker can be running (the worker supports `WORKER_CONCURRENCY=1`
    only): every `running` row then belongs to a dead process. A job with attempts left goes back
    to `queued` (its stages are idempotent, PLAN §8) and its session back to `uploaded`; one that
    used all `max_attempts` is marked `failed` and so is its session. Sessions in any other state
    are left alone.
    """
    stale = db.scalars(
        select(Job).where(Job.status == JobStatus.RUNNING).order_by(Job.id).with_for_update()
    ).all()
    requeued: list[int] = []
    failed: list[int] = []
    for job in stale:
        session = _session_of(db, job)
        in_progress = session is not None and session.status is SessionStatus.PROCESSING
        if job.attempts < max_attempts:
            job.status = JobStatus.QUEUED
            job.started_at = None
            job.log = f"worker died during attempt {job.attempts}; requeued"
            if session is not None and in_progress:
                session.status = SessionStatus.UPLOADED
            requeued.append(job.id)
        else:
            mark_failed(db, job, f"worker died during attempt {job.attempts} of {max_attempts}")
            if session is not None and in_progress:
                session.status = SessionStatus.FAILED
            failed.append(job.id)
    db.flush()
    return Recovery(requeued=requeued, failed=failed)
