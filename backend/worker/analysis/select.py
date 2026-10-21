"""Which sessions an `analyze_video` run uses (PLAN §9).

Inputs are the sessions of a study that watched the video and passed inclusion. Every other session
is listed with the reasons, so `run.json` shows who was left out and why (PLAN §1: fail loudly).
One participant code counts once: the latest included session (DECISIONS 2026-10-19 item 4).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as Db

from app.models import Session as SessionRow
from app.models import SessionStatus
from app.storage import derived_dir
from worker.pipeline.spec import spec_of

# Sessions that finished uploading. Earlier states have no data; withdrawn ones have none left.
UPLOADED_STATES = (
    SessionStatus.UPLOADED,
    SessionStatus.PROCESSING,
    SessionStatus.PROCESSED,
    SessionStatus.FAILED,
)

NOT_PROCESSED = "not_processed"
VIDEO_NOT_WATCHED = "video_not_watched"
GAZE_FILE_MISSING = "gaze_file_missing"
PIPELINE_B_NOT_RUN = "pipeline_b_not_run"
PIPELINE_B_STALE = "pipeline_b_stale"
SUPERSEDED = "superseded"


@dataclass(frozen=True)
class SessionInfo:
    id: uuid.UUID
    participant_code: str
    created_at: datetime
    status: SessionStatus
    quality: dict[str, Any] | None  # `quality.json`, as kept in `sessions.quality`
    has_gaze: bool  # the gaze file of the run's pipeline exists


@dataclass(frozen=True)
class Excluded:
    session_id: uuid.UUID
    participant_code: str
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": str(self.session_id),
            "participant_code": self.participant_code,
            "reasons": self.reasons,
        }


@dataclass(frozen=True)
class Selection:
    included: list[SessionInfo]
    excluded: list[Excluded]


def _frames_of(quality: dict[str, Any], video_id: int) -> int:
    per_video = (quality.get("playback") or {}).get("per_video") or {}
    return int((per_video.get(str(video_id)) or {}).get("frames") or 0)


def _problems(info: SessionInfo, video_id: int, pipeline: str) -> list[str]:
    """Why this session cannot be used for the video; empty when it can. Whether the video was
    watched is a fact about the recording (A's report); the inclusion decision is the pipeline's
    own (B's line in the session record, DECISIONS 2026-10-20)."""
    quality = info.quality
    if info.status is not SessionStatus.PROCESSED or quality is None:
        return [NOT_PROCESSED]
    if _frames_of(quality, video_id) == 0:
        return [VIDEO_NOT_WATCHED]
    inclusion = quality.get("inclusion") or {}
    if pipeline == "b":
        b = quality.get("pipeline_b")
        if b is None:
            return [PIPELINE_B_NOT_RUN]
        if b.get("stale"):
            return [PIPELINE_B_STALE]
        inclusion = {"included": b.get("included"), "reasons": b.get("reasons")}
    if not inclusion.get("included"):
        return [str(r) for r in inclusion.get("reasons") or ["not_included"]]
    if not info.has_gaze:
        return [GAZE_FILE_MISSING]
    return []


def select_sessions(infos: Sequence[SessionInfo], video_id: int, pipeline: str = "a") -> Selection:
    """Split the sessions into the ones to analyse and the ones left out, with reasons."""
    excluded: list[Excluded] = []
    usable: dict[str, list[SessionInfo]] = {}
    for info in sorted(infos, key=lambda i: (i.participant_code, i.created_at, str(i.id))):
        problems = _problems(info, video_id, pipeline)
        if problems:
            excluded.append(Excluded(info.id, info.participant_code, problems))
        else:
            usable.setdefault(info.participant_code, []).append(info)
    included: list[SessionInfo] = []
    for sessions in usable.values():
        *older, latest = sessions  # sorted by creation time
        included.append(latest)
        excluded.extend(Excluded(i.id, i.participant_code, [SUPERSEDED]) for i in older)
    return Selection(included=included, excluded=excluded)


def query_sessions(db: Db, root: Path, study_id: int, pipeline: str = "a") -> list[SessionInfo]:
    rows = db.scalars(
        select(SessionRow).where(
            SessionRow.study_id == study_id, SessionRow.status.in_(UPLOADED_STATES)
        )
    ).all()
    return [
        SessionInfo(
            id=r.id,
            participant_code=r.participant_code,
            created_at=r.created_at,
            status=r.status,
            quality=r.quality,
            has_gaze=(derived_dir(root, study_id, r.id) / spec_of(pipeline).gaze_file).exists(),
        )
        for r in rows
    ]
