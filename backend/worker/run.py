"""Worker process: claims jobs from the DB queue and writes a heartbeat (PLAN §11.1)."""

import logging
import os
import signal
import socket
import threading
import traceback
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.db import make_engine, make_session_factory
from app.jobs import claim_next, mark_done, mark_failed, recover_stale_running
from app.logging import bind_context, configure_logging
from app.models import Job, JobKind, WorkerHeartbeat
from app.settings import Settings
from worker.handlers import make_process_session

log = logging.getLogger("worker")

# A handler runs one job and returns a short log text.
Handler = Callable[[Job], str]


def make_handlers(factory: sessionmaker[Session], settings: Settings) -> dict[JobKind, Handler]:
    return {JobKind.PROCESS_SESSION: make_process_session(factory, settings)}


def beat(db: Session, worker_id: str, now: datetime) -> None:
    stmt = insert(WorkerHeartbeat).values(worker_id=worker_id, beat_at=now)
    db.execute(stmt.on_conflict_do_update(index_elements=["worker_id"], set_={"beat_at": now}))


class Heartbeat(threading.Thread):
    """Updates the heartbeat row every `interval_s`, independent of long-running jobs."""

    def __init__(self, factory: sessionmaker[Session], worker_id: str, interval_s: float) -> None:
        super().__init__(daemon=True, name="heartbeat")
        self._factory = factory
        self._worker_id = worker_id
        self._interval_s = interval_s
        self._stop_event = threading.Event()

    def run(self) -> None:
        while True:
            try:
                with self._factory() as db:
                    beat(db, self._worker_id, datetime.now(UTC))
                    db.commit()
            except Exception:
                log.exception("heartbeat failed")
            if self._stop_event.wait(self._interval_s):
                return

    def stop(self) -> None:
        self._stop_event.set()
        self.join()


def process_one(factory: sessionmaker[Session], handlers: Mapping[JobKind, Handler]) -> bool:
    """Claim and run at most one job. Returns False when the queue is empty."""
    with factory() as db:
        job = claim_next(db)
        db.commit()  # persist `running` and release the row lock
        if job is None:
            return False
        with bind_context(job_id=str(job.id)):
            log.info("job started", extra={"kind": job.kind.value, "attempt": job.attempts})
            handler = handlers.get(job.kind)
            try:
                if handler is None:
                    raise LookupError(f"no handler registered for job kind '{job.kind.value}'")
                result = handler(job)
            except Exception:
                db.rollback()
                mark_failed(db, job, traceback.format_exc())
                db.commit()
                log.exception("job failed")
            else:
                mark_done(db, job, result)
                db.commit()
                log.info("job done")
        return True


def recover_stale_jobs(factory: sessionmaker[Session], max_attempts: int) -> None:
    with factory() as db:
        recovery = recover_stale_running(db, max_attempts)
        db.commit()
    if recovery.requeued or recovery.failed:
        log.warning(
            "stale jobs recovered",
            extra={"requeued": recovery.requeued, "failed": recovery.failed},
        )


def main() -> None:
    settings = Settings()  # type: ignore[call-arg]
    configure_logging(settings.log_level)
    if settings.worker_concurrency != 1:
        raise SystemExit(
            f"WORKER_CONCURRENCY must be 1 (got {settings.worker_concurrency}); "
            "running jobs in parallel is not implemented"
        )
    factory = make_session_factory(make_engine(settings.database_url))
    worker_id = f"{socket.gethostname()}-{os.getpid()}"

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    handlers = make_handlers(factory, settings)
    # Concurrency is 1, so any job still `running` belongs to a worker that died.
    recover_stale_jobs(factory, settings.job_max_attempts)
    heartbeat = Heartbeat(factory, worker_id, settings.worker_heartbeat_interval_s)
    heartbeat.start()
    log.info("worker started", extra={"worker_id": worker_id})
    try:
        while not stop.is_set():
            if not process_one(factory, handlers):
                stop.wait(settings.worker_poll_interval_s)
    finally:
        heartbeat.stop()
        log.info("worker stopped")


if __name__ == "__main__":
    main()
