import threading
from datetime import UTC, datetime

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as DbSession

from app.jobs import claim_next, enqueue, mark_done, mark_failed, recover_stale_running
from app.models import Job, JobKind, JobStatus, SessionStatus, Study, StudyMode
from app.models import Session as SessionRow


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


def _running_job(db: DbSession, attempts: int, args: dict[str, object] | None = None) -> Job:
    job = enqueue(db, JobKind.PROCESS_SESSION, args or {})
    job.status = JobStatus.RUNNING
    job.attempts = attempts
    job.started_at = datetime.now(UTC)
    db.flush()
    return job


def test_recover_requeues_below_max_attempts_and_fails_the_rest(migrated_engine: Engine) -> None:
    with DbSession(migrated_engine) as db:
        again = _running_job(db, attempts=1)
        last = _running_job(db, attempts=3)
        queued = enqueue(db, JobKind.PROCESS_SESSION, {})
        done = enqueue(db, JobKind.PROCESS_SESSION, {})
        mark_done(db, done, "ok")
        db.commit()

        result = recover_stale_running(db, max_attempts=3)
        db.commit()

        assert (result.requeued, result.failed) == ([again.id], [last.id])
        assert again.status is JobStatus.QUEUED
        assert again.started_at is None and again.attempts == 1
        assert "worker died" in again.log and "requeued" in again.log
        assert last.status is JobStatus.FAILED and last.finished_at is not None
        assert "worker died" in last.log and "3 of 3" in last.log
        assert queued.status is JobStatus.QUEUED
        assert done.status is JobStatus.DONE and done.log == "ok"


def test_recover_does_nothing_without_running_jobs(migrated_engine: Engine) -> None:
    with DbSession(migrated_engine) as db:
        enqueue(db, JobKind.PROCESS_SESSION, {})
        db.commit()
        result = recover_stale_running(db, max_attempts=3)
        assert (result.requeued, result.failed) == ([], [])


def test_requeued_job_is_claimed_again_and_counts_the_attempt(migrated_engine: Engine) -> None:
    with DbSession(migrated_engine) as db:
        job = _running_job(db, attempts=1)
        db.commit()
        recover_stale_running(db, max_attempts=3)
        db.commit()
        claimed = claim_next(db)
        assert claimed is not None and claimed.id == job.id and claimed.attempts == 2


def _session_in(db: DbSession, status: SessionStatus) -> SessionRow:
    study = Study(name="S", code="abc", mode=StudyMode.REMOTE, config={"playlist": []})
    db.add(study)
    db.flush()
    session = SessionRow(
        study_id=study.id, participant_code="AB0315", upload_token_hash="h", status=status
    )
    db.add(session)
    db.flush()
    return session


def test_recover_resets_the_session_of_a_requeued_job(migrated_engine: Engine) -> None:
    with DbSession(migrated_engine) as db:
        session = _session_in(db, SessionStatus.PROCESSING)
        _running_job(db, attempts=1, args={"session_id": str(session.id)})
        db.commit()
        recover_stale_running(db, max_attempts=3)
        db.commit()
        assert session.status is SessionStatus.UPLOADED


def test_recover_fails_the_session_of_an_exhausted_job(migrated_engine: Engine) -> None:
    with DbSession(migrated_engine) as db:
        session = _session_in(db, SessionStatus.PROCESSING)
        _running_job(db, attempts=3, args={"session_id": str(session.id)})
        db.commit()
        recover_stale_running(db, max_attempts=3)
        db.commit()
        assert session.status is SessionStatus.FAILED


def test_recover_leaves_other_session_states_alone(migrated_engine: Engine) -> None:
    # A session that is already `processed` (or withdrawn) must not be pulled back by a stale job.
    with DbSession(migrated_engine) as db:
        session = _session_in(db, SessionStatus.WITHDRAWN)
        _running_job(db, attempts=1, args={"session_id": str(session.id)})
        db.commit()
        recover_stale_running(db, max_attempts=3)
        db.commit()
        assert session.status is SessionStatus.WITHDRAWN
