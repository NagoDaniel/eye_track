"""Queue an `analyze_video` run (PLAN §6, §9).

The admin page and the CLI end here. The worker does the work; nothing runs in the api process.
A run has an id from the moment it is queued; its directory and `run.json` appear when the worker
starts it.
"""

import math
import secrets
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session as Db

from app.jobs import enqueue
from app.models import Job, JobKind, JobStatus, Study, Video
from app.service_errors import ConflictError, InvalidError, NotFoundError

RUN_FILE = "run.json"
GAZE_COLUMNS = ("plain", "drift_corrected")
PIPELINES = ("a", "b")
OPEN_JOBS = (JobStatus.QUEUED, JobStatus.RUNNING)


def new_run_id(now: datetime | None = None) -> str:
    """Sorts by time; the random tail keeps two runs in the same second apart."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


def analysis_jobs(db: Db, study_id: int | None = None, video_id: int | None = None) -> list[Job]:
    """The `analyze_video` jobs, newest first, optionally of one study and video."""
    stmt = select(Job).where(Job.kind == JobKind.ANALYZE_VIDEO).order_by(Job.id.desc())
    if study_id is not None:
        stmt = stmt.where(Job.args["study_id"].as_integer() == study_id)
    if video_id is not None:
        stmt = stmt.where(Job.args["video_id"].as_integer() == video_id)
    return list(db.scalars(stmt))


def queue_analysis(
    db: Db,
    study_id: int,
    video_id: int,
    gaze_columns: str = "drift_corrected",
    aoi_tolerance_k: float = 0.0,
    heatmap_dots: bool = False,
    pipeline: str = "a",
) -> tuple[Job, str]:
    """Queue `analyze_video`. The caller commits. Returns the job and the run id.

    Raises NotFoundError, InvalidError (options) or ConflictError (a run of this video of this
    study is already queued or running)."""
    if db.get(Study, study_id) is None:
        raise NotFoundError(f"study {study_id} not found")
    if db.get(Video, video_id) is None:
        raise NotFoundError(f"video {video_id} not found")
    if gaze_columns not in GAZE_COLUMNS:
        raise InvalidError(f"gaze_columns must be one of: {', '.join(GAZE_COLUMNS)}")
    if pipeline not in PIPELINES:
        raise InvalidError(f"pipeline must be one of: {', '.join(PIPELINES)}")
    if not math.isfinite(aoi_tolerance_k) or aoi_tolerance_k < 0:
        raise InvalidError("aoi_tolerance_k must be a number of at least 0")
    if any(j.status in OPEN_JOBS for j in analysis_jobs(db, study_id, video_id)):
        raise ConflictError("an analysis of this video in this study is already queued or running")
    run_id = new_run_id()
    job = enqueue(
        db,
        JobKind.ANALYZE_VIDEO,
        {
            "study_id": study_id,
            "video_id": video_id,
            "run_id": run_id,
            "gaze_columns": gaze_columns,
            "aoi_tolerance_k": aoi_tolerance_k,
            "heatmap_dots": heatmap_dots,
            "pipeline": pipeline,
        },
    )
    return job, run_id


def compare_jobs(db: Db, study_id: int | None = None) -> list[Job]:
    """The `compare_pipelines` jobs, newest first, optionally of one study."""
    stmt = select(Job).where(Job.kind == JobKind.COMPARE_PIPELINES).order_by(Job.id.desc())
    if study_id is not None:
        stmt = stmt.where(Job.args["study_id"].as_integer() == study_id)
    return list(db.scalars(stmt))


def queue_comparison(db: Db, study_id: int) -> tuple[Job, str]:
    """Queue `compare_pipelines` for a study. The caller commits. Returns the job and the run id.

    Raises NotFoundError or ConflictError (a comparison of this study is already queued or
    running)."""
    if db.get(Study, study_id) is None:
        raise NotFoundError(f"study {study_id} not found")
    if any(j.status in OPEN_JOBS for j in compare_jobs(db, study_id)):
        raise ConflictError("a comparison of this study is already queued or running")
    run_id = new_run_id()
    job = enqueue(db, JobKind.COMPARE_PIPELINES, {"study_id": study_id, "run_id": run_id})
    return job, run_id
