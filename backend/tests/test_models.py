import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.db import Base
from app.models import (
    AdminUser,
    Chunk,
    ChunkStream,
    Job,
    JobKind,
    JobStatus,
    Session,
    SessionStatus,
    Study,
    StudyMode,
)
from tests.dbutil import downgrade, upgrade

EXPECTED_TABLES = {
    "studies", "videos", "stations", "sessions", "chunks", "jobs", "admin_users",
    "worker_heartbeats",
}  # fmt: skip


def tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def make_study(db: DbSession) -> Study:
    study = Study(name="S", code="abc", mode=StudyMode.REMOTE, config={"playlist": []})
    db.add(study)
    db.flush()
    return study


def make_session(db: DbSession, study: Study) -> Session:
    s = Session(study_id=study.id, participant_code="p1", upload_token_hash="h")
    db.add(s)
    db.flush()
    return s


def test_upgrade_creates_and_downgrade_removes_all_tables(engine: Engine) -> None:
    upgrade(engine)
    assert tables(engine) == EXPECTED_TABLES
    downgrade(engine)
    assert tables(engine) == set()


def test_migrations_match_models(engine: Engine) -> None:
    upgrade(engine)
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == []


def test_postgres_major_version_is_16(engine: Engine) -> None:
    with engine.connect() as conn:
        assert str(conn.execute(text("show server_version")).scalar()).startswith("16.")


def test_chunk_primary_key_is_session_stream_seq(engine: Engine) -> None:
    upgrade(engine)
    pk = inspect(engine).get_pk_constraint("chunks")["constrained_columns"]
    assert pk == ["session_id", "stream", "seq"]
    with DbSession(engine) as db:
        s = make_session(db, make_study(db))
        db.add(Chunk(session_id=s.id, stream=ChunkStream.WEBCAM, seq=0, size=1, sha256="a"))
        db.add(Chunk(session_id=s.id, stream=ChunkStream.EVENTS, seq=0, size=1, sha256="a"))
        db.flush()  # same seq on a different stream is fine
        db.add(Chunk(session_id=s.id, stream=ChunkStream.WEBCAM, seq=0, size=2, sha256="b"))
        with pytest.raises(IntegrityError):
            db.flush()


def test_session_defaults(engine: Engine) -> None:
    upgrade(engine)
    with DbSession(engine) as db:
        s = make_session(db, make_study(db))
        db.refresh(s)
        assert isinstance(s.id, uuid.UUID)
        assert s.status is SessionStatus.CREATED
        assert s.flags == []
        assert s.is_debug is False
        assert s.created_at is not None


def test_invalid_status_rejected_by_check_constraint(engine: Engine) -> None:
    upgrade(engine)
    with DbSession(engine) as db:
        s = make_session(db, make_study(db))
        with pytest.raises((IntegrityError, DataError)):
            db.execute(text("update sessions set status = 'bogus' where id = :id"), {"id": s.id})


def test_session_requires_existing_study(engine: Engine) -> None:
    upgrade(engine)
    with DbSession(engine) as db:
        db.add(Session(study_id=999, participant_code="p", upload_token_hash="h"))
        with pytest.raises(IntegrityError):
            db.flush()


def test_study_code_unique(engine: Engine) -> None:
    upgrade(engine)
    with DbSession(engine) as db:
        make_study(db)
        db.add(Study(name="T", code="abc", mode=StudyMode.LAB, config={}))
        with pytest.raises(IntegrityError):
            db.flush()


def test_job_defaults_and_admin_username_unique(engine: Engine) -> None:
    upgrade(engine)
    with DbSession(engine) as db:
        job = Job(kind=JobKind.PROCESS_SESSION, args={"session_id": "x"})
        db.add(job)
        db.add(AdminUser(username="a", password_hash="h"))
        db.flush()
        db.refresh(job)
        assert job.status is JobStatus.QUEUED and job.attempts == 0 and job.log == ""
        db.add(AdminUser(username="a", password_hash="h2"))
        with pytest.raises(IntegrityError):
            db.flush()


def test_job_kinds_are_checked_and_render_debug_is_one_of_them(engine: Engine) -> None:
    upgrade(engine)
    with DbSession(engine) as db:
        db.add(Job(kind=JobKind.RENDER_DEBUG, args={"session_id": "x"}))
        db.flush()
        with pytest.raises((IntegrityError, DataError)):
            db.execute(text("insert into jobs (kind, args, status, attempts, log) "
                            "values ('bogus', '{}', 'queued', 0, '')"))  # fmt: skip
        db.rollback()


def test_downgrading_to_the_first_revision_drops_replay_jobs_and_the_kind(engine: Engine) -> None:
    upgrade(engine)
    with DbSession(engine) as db:
        db.add(Job(kind=JobKind.PROCESS_SESSION, args={}))
        db.add(Job(kind=JobKind.RENDER_DEBUG, args={}))
        db.commit()
    downgrade(engine, "0001")
    with DbSession(engine) as db:
        assert db.execute(text("select kind from jobs")).scalars().all() == ["process_session"]
        with pytest.raises((IntegrityError, DataError)):
            db.execute(text("insert into jobs (kind, args, status, attempts, log) "
                            "values ('render_debug', '{}', 'queued', 0, '')"))  # fmt: skip
        db.rollback()
    upgrade(engine)  # and up again
    with DbSession(engine) as db:
        db.add(Job(kind=JobKind.RENDER_DEBUG, args={}))
        db.commit()
