"""Pipeline B's names, stale mark and queueing, in one light module (PLAN §8.5, §6; DECISIONS
2026-10-20): the admin pages (api process) need them without importing the pipeline.

Pipeline B is a job of its own (`process_pipeline_b`): the researcher decides when to pay for it.
Its stages write `derived/features_b.parquet`, `calibration_b.json`, `gaze_b.parquet`,
`fixations_b.parquet`, `quality_b.json` and `stages_b.json`. B's inputs are the landmarks, A's
features and the sync of A's pipeline: when A runs one of the stages that make them again, B's
files no longer fit and B is marked **stale** (`pipeline_b_stale.json`) until B is run again from
`features_b`.
"""

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session as Db

from app.jobs import enqueue
from app.models import Job, JobKind, JobStatus, SessionStatus
from app.models import Session as SessionRow
from app.reprocess import jobs_of
from app.service_errors import ConflictError, InvalidError, NotFoundError
from app.storage import atomic_write_bytes, derived_dir

FEATURES_B_FILE = "features_b.parquet"
CALIBRATION_B_FILE = "calibration_b.json"
GAZE_B_FILE = "gaze_b.parquet"
FIXATIONS_B_FILE = "fixations_b.parquet"
QUALITY_B_FILE = "quality_b.json"
STAGES_B_FILE = "stages_b.json"
STALE_FILE = "pipeline_b_stale.json"

# In the order the B job runs them (the plots, `plots_b`, are added with the B plots, PLAN §12.2).
B_STAGE_NAMES = ("features_b", "calibration_b", "gaze_b", "quality_b")
B_STAGE_DESCRIPTIONS = {
    "features_b": "gaze angles of the neural network on a face crop of every frame",
    "calibration_b": "fit the gaze model on B's features",
    "gaze_b": "predict, smooth, fixations, video coordinates (Pipeline B)",
    "quality_b": "B's quality report and inclusion decision",
}
# The stages of Pipeline A whose output B reads: running one makes B stale.
A_STAGES_FEEDING_B = ("ingest", "landmarks", "features", "sync")

OPEN_JOBS = (JobStatus.QUEUED, JobStatus.RUNNING)
# A session's B files only make sense once A has made what B reads.
NEEDED_FROM_A = ("landmarks.parquet", "features_a.parquet", "sync.json")


def mark_stale(derived: Path, stage: str) -> bool:
    """Mark B stale because A ran `stage` (only when B has files). True when it was marked."""
    if not (derived / FEATURES_B_FILE).exists():
        return False
    document = {"since": datetime.now(UTC).isoformat(), "by_stage": stage}
    atomic_write_bytes(derived / STALE_FILE, (json.dumps(document) + "\n").encode())
    return True


def stale_info(derived: Path) -> dict[str, Any] | None:
    path = derived / STALE_FILE
    if not path.is_file():
        return None
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return document


def clear_stale(derived: Path) -> None:
    (derived / STALE_FILE).unlink(missing_ok=True)


def weights_refusal(models_root: Path, model_name: str) -> str | None:
    """Why the weights of `model_name` are not usable, or None. Looks at the file only (the hash is
    checked when the model is loaded)."""
    from worker.pipeline.model_b import ModelBError, manifest_entry  # heavy: onnxruntime

    try:
        entry = manifest_entry(model_name)
    except ModelBError as exc:
        return str(exc)
    if not (models_root / entry["file"]).is_file():
        return (
            f"the weights {entry['file']} are not in {models_root}; "
            f"download them with scripts/fetch_models.sh {model_name}"
        )
    return None


def refusal(
    db: Db,
    session: SessionRow,
    data_root: Path,
    weights: tuple[Path, str] | None = None,
) -> str | None:
    """Why Pipeline B cannot be queued for this session now, or None. `weights` is the directory
    and name of the model to check for (the admin and the CLI give it; the job checks again)."""
    if session.status is not SessionStatus.PROCESSED:
        return f"the session is {session.status.value}; Pipeline A must have processed it first"
    derived = derived_dir(data_root, session.study_id, session.id)
    missing = [name for name in NEEDED_FROM_A if not (derived / name).is_file()]
    if missing:
        return f"Pipeline A has not made {', '.join(missing)} for this session"
    if any(job.status in OPEN_JOBS for job in jobs_of(db, session.id)):
        return "a job for this session is already queued or running"
    return None if weights is None else weights_refusal(*weights)


def queue_pipeline_b(
    db: Db,
    session_id: uuid.UUID,
    data_root: Path,
    from_stage: str | None = None,
    weights: tuple[Path, str] | None = None,
) -> Job:
    """Queue `process_pipeline_b` (from `from_stage`: None for the whole pipeline). The caller
    commits. Raises NotFoundError, InvalidError (unknown stage) or ConflictError (not now)."""
    session = db.get(SessionRow, session_id)
    if session is None:
        raise NotFoundError(f"session {session_id} not found")
    if from_stage is not None and from_stage not in B_STAGE_NAMES:
        raise InvalidError(f"unknown stage '{from_stage}' (stages: {', '.join(B_STAGE_NAMES)})")
    reason = refusal(db, session, data_root, weights)
    if reason is not None:
        raise ConflictError(reason)
    args: dict[str, object] = {"session_id": str(session_id)}
    if from_stage is not None:
        args["from_stage"] = from_stage
    return enqueue(db, JobKind.PROCESS_PIPELINE_B, args)


def summary_of(derived: Path) -> dict[str, Any] | None:
    """B's line in the session record (`sessions.quality["pipeline_b"]`): its own inclusion, the
    network, the fit, the valid fraction and the validation error, and whether it is stale. None
    when B has no report."""
    path = derived / QUALITY_B_FILE
    if not path.is_file():
        return None
    report: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    playback = report.get("playback") or {}
    validation = report.get("validation") or {}
    return {
        "included": report["inclusion"]["included"],
        "reasons": report["inclusion"]["reasons"],
        "model": report["features"]["model"].get("name"),
        "calibration_status": report["calibration"]["status"],
        "valid_fraction": playback.get("valid_fraction"),
        "validation_error_norm": validation.get("mean_error_norm"),
        "stale": stale_info(derived) is not None,
    }


def queue_study(
    db: Db,
    study_id: int,
    data_root: Path,
    weights: tuple[Path, str] | None = None,
) -> tuple[list[Job], list[tuple[SessionRow, str]]]:
    """Queue Pipeline B for every processed session of the study that can take it. Returns the
    jobs and, for the processed sessions that cannot, the reason. The caller commits."""
    from sqlalchemy import select

    queued: list[Job] = []
    left: list[tuple[SessionRow, str]] = []
    for session in db.scalars(
        select(SessionRow).where(SessionRow.study_id == study_id).order_by(SessionRow.created_at)
    ):
        if session.status is not SessionStatus.PROCESSED:
            continue  # not Pipeline B's business yet
        reason = refusal(db, session, data_root, weights)
        if reason is None:
            queued.append(queue_pipeline_b(db, session.id, data_root, None, weights))
        else:
            left.append((session, reason))
    return queued, left
