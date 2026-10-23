"""Stopping a running job when the admin asks (DECISIONS 2026-10-22).

The api process cannot reach into the worker, so the admin sets `jobs.cancel_requested` and the
worker looks at it between its steps: `check_cancelled()` is called by the stage runners, between
the batches of the gaze network, every so many frames of the landmark and heatmap loops, and raises
`JobCancelled`, which ends the job as failed with the log "cancelled by the admin". A step that
does not look (the middle of an encode, say) finishes first; the stop comes within seconds for the
long loops and at the end of the step for the others. Outside a job (CLI, tests) it does nothing.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from app.jobs import CANCELLED_LOG
from app.models import Job

__all__ = ["CANCELLED_LOG", "JobCancelled", "check_cancelled", "job_context"]
POLL_EVERY_S = 1.0  # how often the flag is looked up at most


class JobCancelled(Exception):
    """The admin asked for this job to stop."""


@dataclass
class _Current:
    factory: sessionmaker[Session]
    job_id: int
    last_poll: float = 0.0


_current: ContextVar[_Current | None] = ContextVar("current_job", default=None)


@contextmanager
def job_context(factory: sessionmaker[Session], job_id: int) -> Iterator[None]:
    """The job the worker is running now, for `check_cancelled`."""
    token = _current.set(_Current(factory, job_id))
    try:
        yield
    finally:
        _current.reset(token)


def check_cancelled(force: bool = False) -> None:
    """Raise `JobCancelled` when the admin asked for the running job to stop. Cheap to call: the
    database is asked at most once per `POLL_EVERY_S` (always with `force`)."""
    current = _current.get()
    if current is None:
        return
    now = time.monotonic()
    if not force and now - current.last_poll < POLL_EVERY_S:
        return
    current.last_poll = now
    with current.factory() as db:
        job = db.get(Job, current.job_id)
        if job is not None and job.cancel_requested:
            raise JobCancelled(CANCELLED_LOG)
