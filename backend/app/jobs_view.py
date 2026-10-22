"""The jobs of a study for the admin, and stopping them (DECISIONS 2026-10-22).

A job belongs to a study through its arguments: a session (processing, Pipeline B, debug replay)
or the study itself (analysis, comparison). Light module: the api process reads it.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as Db

from app.models import Job, JobKind, JobStatus, Video
from app.models import Session as SessionRow

OPEN = (JobStatus.QUEUED, JobStatus.RUNNING)
RECENT = 200  # finished jobs looked at to find the last ones of a study
KIND_LABELS = {
    JobKind.PROCESS_SESSION: "Processing (Pipeline A)",
    JobKind.PROCESS_PIPELINE_B: "Pipeline B",
    JobKind.RENDER_DEBUG: "Debug replay",
    JobKind.ANALYZE_VIDEO: "Analysis",
    JobKind.COMPARE_PIPELINES: "Pipeline comparison",
}


def _candidates(db: Db) -> list[Job]:
    """Every open job and the most recent ones, newest first."""
    open_jobs = db.scalars(select(Job).where(Job.status.in_(OPEN))).all()
    recent = db.scalars(select(Job).order_by(Job.id.desc()).limit(RECENT)).all()
    merged = {j.id: j for j in (*recent, *open_jobs)}
    return sorted(merged.values(), key=lambda j: j.id, reverse=True)


def _session_ids(jobs: list[Job]) -> dict[str, uuid.UUID]:
    found: dict[str, uuid.UUID] = {}
    for job in jobs:
        raw = job.args.get("session_id")
        if isinstance(raw, str):
            try:
                found[raw] = uuid.UUID(raw)
            except ValueError:
                continue
    return found


def _rows(db: Db, jobs: list[Job]) -> list[dict[str, Any]]:
    ids = _session_ids(jobs)
    sessions: dict[str, SessionRow] = {}
    if ids:
        for row in db.scalars(select(SessionRow).where(SessionRow.id.in_(ids.values()))):
            sessions[str(row.id)] = row
    videos = {v.id: v.title for v in db.scalars(select(Video))}
    out = []
    for job in jobs:
        session = sessions.get(str(job.args.get("session_id")))
        study_id = session.study_id if session is not None else job.args.get("study_id")
        if not isinstance(study_id, int):
            continue  # a job whose study cannot be told (its session is gone)
        video_id = job.args.get("video_id")
        if session is not None:
            what = f"{session.participant_code} ({str(session.id)[:8]})"
        elif isinstance(video_id, int):
            what = f"video: {videos.get(video_id, video_id)}"
        else:
            what = "the whole study"
        out.append(
            {
                "id": job.id,
                "study_id": study_id,
                "kind": job.kind.value,
                "label": KIND_LABELS[job.kind],
                "status": job.status.value,
                "cancel_requested": job.cancel_requested,
                "cancellable": job.status in OPEN and not job.cancel_requested,
                "attempts": job.attempts,
                "what": what,
                "session_id": None if session is None else str(session.id),
                "run_id": job.args.get("run_id"),
                "from_stage": job.args.get("from_stage") or job.args.get("only"),
                "created_at": job.created_at.isoformat(),
                "started_at": None if job.started_at is None else job.started_at.isoformat(),
                "finished_at": None if job.finished_at is None else job.finished_at.isoformat(),
                "log": job.log[-1500:],
            }
        )
    return out


def jobs_of_study(db: Db, study_id: int, finished: int = 10) -> list[dict[str, Any]]:
    """The open jobs of the study (running first, then queued, oldest first) and its last
    `finished` finished ones, newest first."""
    rows = [r for r in _rows(db, _candidates(db)) if r["study_id"] == study_id]
    open_rows = [r for r in rows if r["status"] in ("queued", "running")]
    open_rows.sort(key=lambda r: (r["status"] != "running", r["id"]))
    done = [r for r in rows if r["status"] not in ("queued", "running")][:finished]
    return open_rows + done


def open_counts(db: Db) -> dict[int, dict[str, int]]:
    """Per study how many jobs run and wait."""
    counts: dict[int, dict[str, int]] = {}
    open_jobs = list(db.scalars(select(Job).where(Job.status.in_(OPEN))))
    for row in _rows(db, open_jobs):
        entry = counts.setdefault(row["study_id"], {"running": 0, "queued": 0})
        entry[row["status"]] += 1
    return counts
