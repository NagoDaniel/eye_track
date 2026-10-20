"""Process a session again from a stage, as a queued job (PLAN §6, §12.2).

The admin page's "re-run from this stage" button and the JSON API both end here. The worker does
the work (`process_session` with `from_stage`); nothing runs in the api process.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session as Db

from app.jobs import enqueue
from app.models import Job, JobKind, JobStatus, SessionStatus
from app.models import Session as SessionRow
from app.pipeline_names import STAGE_NAMES
from app.service_errors import ConflictError, InvalidError, NotFoundError

# A session can be processed again from any of these (the same list the worker accepts).
RERUNNABLE = (SessionStatus.UPLOADED, SessionStatus.PROCESSED, SessionStatus.FAILED)
OPEN_JOBS = (JobStatus.QUEUED, JobStatus.RUNNING)


JOB_KINDS = (JobKind.PROCESS_SESSION, JobKind.RENDER_DEBUG, JobKind.PROCESS_PIPELINE_B)


def jobs_of(db: Db, session_id: uuid.UUID) -> list[Job]:
    """The processing and replay jobs of a session, newest first."""
    return list(
        db.scalars(
            select(Job)
            .where(
                Job.kind.in_(JOB_KINDS),
                Job.args["session_id"].as_string() == str(session_id),
            )
            .order_by(Job.id.desc())
        )
    )


def rerun_refusal(db: Db, session: SessionRow) -> str | None:
    """Why the session cannot be processed again now, or None."""
    if session.status not in RERUNNABLE:
        return (
            f"the session is {session.status.value}; "
            "only uploaded, processed or failed ones can be run again"
        )
    if any(job.status in OPEN_JOBS for job in jobs_of(db, session.id)):
        return "a job for this session is already queued or running"
    return None


def queue_rerun(db: Db, session_id: uuid.UUID, from_stage: str | None) -> Job:
    """Queue `process_session` from `from_stage` (None: from the start). The caller commits.

    Raises NotFoundError, InvalidError (unknown stage) or ConflictError (not now).
    """
    session = db.get(SessionRow, session_id)
    if session is None:
        raise NotFoundError(f"session {session_id} not found")
    if from_stage is not None and from_stage not in STAGE_NAMES:
        raise InvalidError(f"unknown stage '{from_stage}' (stages: {', '.join(STAGE_NAMES)})")
    refusal = rerun_refusal(db, session)
    if refusal is not None:
        raise ConflictError(refusal)
    args: dict[str, object] = {"session_id": str(session_id)}
    if from_stage is not None:
        args["from_stage"] = from_stage
    return enqueue(db, JobKind.PROCESS_SESSION, args)


def render_refusal(db: Db, session: SessionRow, has_gaze: bool) -> str | None:
    """Why the debug replay cannot be rendered now, or None."""
    if session.status != SessionStatus.PROCESSED:
        return f"the session is {session.status.value}; only a processed session can be replayed"
    if not has_gaze:
        return "the session has no gaze (its calibration failed?), so there is nothing to replay"
    if any(job.status in OPEN_JOBS for job in jobs_of(db, session.id)):
        return "a job for this session is already queued or running"
    return None


def queue_render(db: Db, session_id: uuid.UUID, has_gaze: bool) -> Job:
    """Queue `render_debug` for a processed session. The caller commits.

    Raises NotFoundError or ConflictError (not now)."""
    session = db.get(SessionRow, session_id)
    if session is None:
        raise NotFoundError(f"session {session_id} not found")
    refusal = render_refusal(db, session, has_gaze)
    if refusal is not None:
        raise ConflictError(refusal)
    return enqueue(db, JobKind.RENDER_DEBUG, {"session_id": str(session_id)})
