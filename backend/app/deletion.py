"""Deleting a session, or a study with everything in it (DECISIONS 2026-11-02).

For test data and for participants who withdraw. Removes the database rows (the session, its
chunks and its jobs; for a study also the analysis and comparison jobs) and the files on disk
(`studies/<id>/sessions/<session>/`, for a study also `studies/<id>/` and `analysis/<id>/`).
Stimulus videos belong to no study and stay. Nothing is deleted while a job of the session or
study is queued or running: the worker would be writing into the directory.

The caller commits. The rows are deleted first and the files after; if removing the files fails the
exception rolls the rows back, and deleting again finishes the job.
"""

import logging
import shutil
import uuid
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session as Db

from app.analysis_runs import OPEN_JOBS
from app.models import Chunk, Job, JobKind, Study
from app.models import Session as SessionRow
from app.service_errors import ConflictError, InvalidError, NotFoundError
from app.storage import session_dir

log = logging.getLogger("admin")

SESSION_JOB_KINDS = (JobKind.PROCESS_SESSION, JobKind.PROCESS_PIPELINE_B, JobKind.RENDER_DEBUG)
STUDY_JOB_KINDS = (JobKind.ANALYZE_VIDEO, JobKind.COMPARE_PIPELINES)


def _session_jobs(db: Db, session_ids: list[uuid.UUID]) -> list[Job]:
    ids = [str(s) for s in session_ids]
    stmt = select(Job).where(
        Job.kind.in_(SESSION_JOB_KINDS), Job.args["session_id"].as_string().in_(ids)
    )
    return list(db.scalars(stmt))


def _study_jobs(db: Db, study_id: int) -> list[Job]:
    stmt = select(Job).where(
        Job.kind.in_(STUDY_JOB_KINDS), Job.args["study_id"].as_integer() == study_id
    )
    return list(db.scalars(stmt))


def _refuse_open(jobs: list[Job], what: str) -> None:
    open_jobs = [j for j in jobs if j.status in OPEN_JOBS]
    if open_jobs:
        raise ConflictError(
            f"{what} has {len(open_jobs)} job(s) queued or running; "
            "wait until they have finished (or cancel them) and try again"
        )


def _remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def _delete_rows(db: Db, sessions: list[SessionRow], jobs: list[Job]) -> int:
    """The sessions, their chunks and the jobs; returns the number of chunk rows."""
    chunks = 0
    if sessions:
        result = db.execute(delete(Chunk).where(Chunk.session_id.in_([s.id for s in sessions])))
        chunks = int(result.rowcount or 0)  # type: ignore[attr-defined]
    for row in (*jobs, *sessions):
        db.delete(row)
    db.flush()
    return chunks


def delete_session(db: Db, root: Path, session_id: uuid.UUID) -> None:
    """Delete one session: rows and files. `NotFoundError`, or `ConflictError` while it is being
    processed. Analysis runs that used the session are not changed (they hold aggregates)."""
    session = db.get(SessionRow, session_id)
    if session is None:
        raise NotFoundError(f"session {session_id} not found")
    jobs = _session_jobs(db, [session.id])
    _refuse_open(jobs, f"session {session_id}")
    study_id, code = session.study_id, session.participant_code
    chunks = _delete_rows(db, [session], jobs)
    _remove_tree(session_dir(root, study_id, session_id))
    log.info(
        "session deleted",
        extra={"session_id": str(session_id), "study_id": study_id, "participant": code,
               "chunk_rows": chunks, "jobs": len(jobs)},
    )  # fmt: skip


def delete_study_with_data(db: Db, root: Path, study_id: int, confirm_code: str) -> int:
    """Delete a study with all its sessions, jobs and analysis outputs; `confirm_code` must be the
    study's code (typed by the admin). Returns the number of sessions deleted. Raises
    `NotFoundError`, `InvalidError` (wrong code) or `ConflictError` (open jobs)."""
    study = db.get(Study, study_id)
    if study is None:
        raise NotFoundError(f"study {study_id} not found")
    if confirm_code != study.code:
        raise InvalidError("the study code typed does not match; nothing was deleted")
    sessions = list(db.scalars(select(SessionRow).where(SessionRow.study_id == study_id)))
    jobs = _session_jobs(db, [s.id for s in sessions]) + _study_jobs(db, study_id)
    _refuse_open(jobs, f"study {study_id}")
    code = study.code
    _delete_rows(db, sessions, jobs)
    db.delete(study)
    db.flush()
    _remove_tree(root / "studies" / str(study_id))
    _remove_tree(root / "analysis" / str(study_id))
    log.info(
        "study deleted with its data",
        extra={"study_id": study_id, "study_code": code, "sessions": len(sessions)},
    )
    return len(sessions)
