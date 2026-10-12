import threading

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as DbSession

from app.jobs import claim_next, enqueue, mark_done, mark_failed
from app.models import Job, JobKind, JobStatus


def test_enqueue_creates_queued_job(migrated_engine: Engine) -> None:
    with DbSession(migrated_engine) as db:
        job = enqueue(db, JobKind.PROCESS_SESSION, {"session_id": "abc"})
        db.commit()
        assert job.status is JobStatus.QUEUED
        assert job.args == {"session_id": "abc"}
        assert job.attempts == 0


def test_claim_returns_oldest_queued_and_marks_running(migrated_engine: Engine) -> None:
    with DbSession(migrated_engine) as db:
        first = enqueue(db, JobKind.PROCESS_SESSION, {"n": 1})
        enqueue(db, JobKind.ANALYZE_VIDEO, {"n": 2})
        db.commit()
        claimed = claim_next(db)
        assert claimed is not None and claimed.id == first.id
        assert claimed.status is JobStatus.RUNNING
        assert claimed.attempts == 1
        assert claimed.started_at is not None


def test_claim_empty_queue_returns_none(migrated_engine: Engine) -> None:
    with DbSession(migrated_engine) as db:
        assert claim_next(db) is None


def test_running_job_is_not_claimed_again(migrated_engine: Engine) -> None:
    with DbSession(migrated_engine) as db:
        enqueue(db, JobKind.PROCESS_SESSION, {})
        db.commit()
        assert claim_next(db) is not None
        db.commit()
        assert claim_next(db) is None


def test_done_and_failed_record_outcome(migrated_engine: Engine) -> None:
    with DbSession(migrated_engine) as db:
        a = enqueue(db, JobKind.PROCESS_SESSION, {})
        b = enqueue(db, JobKind.PROCESS_SESSION, {})
        db.commit()
        mark_done(db, a, "ok")
        mark_failed(db, b, "boom")
        db.commit()
        assert (a.status, a.log) == (JobStatus.DONE, "ok")
        assert (b.status, b.log) == (JobStatus.FAILED, "boom")
        assert a.finished_at is not None and b.finished_at is not None


def test_skip_locked_second_claimer_does_not_block_or_double_claim(
    migrated_engine: Engine,
) -> None:
    with DbSession(migrated_engine) as seed:
        j1 = enqueue(seed, JobKind.PROCESS_SESSION, {"n": 1})
        j2 = enqueue(seed, JobKind.PROCESS_SESSION, {"n": 2})
        seed.commit()
        id1, id2 = j1.id, j2.id
    with DbSession(migrated_engine) as w1, DbSession(migrated_engine) as w2:
        c1 = claim_next(w1)  # transaction still open: row 1 stays locked
        c2 = claim_next(w2)  # must skip row 1, not wait for it
        assert c1 is not None and c2 is not None
        assert (c1.id, c2.id) == (id1, id2)
        w1.commit()
        w2.commit()


def test_concurrent_claimers_never_share_a_job(migrated_engine: Engine) -> None:
    n_jobs, n_workers = 40, 6
    with DbSession(migrated_engine) as seed:
        for i in range(n_jobs):
            enqueue(seed, JobKind.PROCESS_SESSION, {"n": i})
        seed.commit()

    claimed: list[int] = []
    lock = threading.Lock()

    def worker() -> None:
        while True:
            with DbSession(migrated_engine) as db:
                job = claim_next(db)
                db.commit()
                if job is None:
                    return
                with lock:
                    claimed.append(job.id)

    threads = [threading.Thread(target=worker) for _ in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == n_jobs
    assert len(set(claimed)) == n_jobs
    with DbSession(migrated_engine) as db:
        rows = db.scalars(select(Job)).all()
        assert all(j.status is JobStatus.RUNNING and j.attempts == 1 for j in rows)
