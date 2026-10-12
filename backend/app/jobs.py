"""DB-backed job queue (PLAN §4). Workers claim with `SELECT … FOR UPDATE SKIP LOCKED`.

Functions only flush; the caller owns the transaction and must commit.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Job, JobKind, JobStatus


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
