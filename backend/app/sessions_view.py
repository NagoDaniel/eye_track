"""Read-only views of recorded sessions for the admin (PLAN §6, DECISIONS 1b item 12).

Gaps in the chunk sequence are reported, never hidden: they are what tells the researcher that a
participant's upload is incomplete.
"""

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.events import assemble_events, parse_events
from app.models import Chunk, Session, Study
from app.pipeline_names import PLOT_CAPTIONS, REPLAY_FILE, STAGE_DESCRIPTIONS, STAGE_NAMES
from app.reprocess import jobs_of, render_refusal, rerun_refusal
from app.schemas import EventRecord
from app.service_errors import NotFoundError
from app.stage_status import read_entries
from app.storage import STREAMS, chunk_path, derived_dir, webcam_path

MAX_EVENTS_SHOWN = 1000


def inclusion_of(quality: dict[str, Any] | None) -> dict[str, Any] | None:
    """The inclusion decision stored in a session's quality report, if there is one."""
    inclusion = None if quality is None else quality.get("inclusion")
    return inclusion if isinstance(inclusion, dict) else None


def _row(session: Session, study: Study) -> dict[str, Any]:
    inclusion = inclusion_of(session.quality)
    return {
        "id": str(session.id),
        "study_id": study.id,
        "study_name": study.name,
        "study_code": study.code,
        "participant_code": session.participant_code,
        "status": session.status.value,
        "flags": session.flags,
        "created_at": session.created_at.isoformat(),
        "included": None if inclusion is None else inclusion.get("included"),
        "inclusion_reasons": [] if inclusion is None else inclusion.get("reasons", []),
    }


def list_sessions(db: DbSession, study_id: int | None = None) -> list[dict[str, Any]]:
    """Newest first; each row has the number of chunks received per stream."""
    query = select(Session, Study).join(Study, Study.id == Session.study_id)
    counts = select(Chunk.session_id, Chunk.stream, func.count()).group_by(
        Chunk.session_id, Chunk.stream
    )
    if study_id is not None:
        query = query.where(Session.study_id == study_id)
        counts = counts.join(Session, Session.id == Chunk.session_id).where(
            Session.study_id == study_id
        )
    chunk_counts: dict[uuid.UUID, dict[str, int]] = {}
    for session_id, stream, n in db.execute(counts):
        chunk_counts.setdefault(session_id, {})[stream.value] = n
    rows = []
    for session, study in db.execute(query.order_by(Session.created_at.desc(), Session.id)):
        per_stream = chunk_counts.get(session.id, {})
        rows.append({**_row(session, study), "chunks": {s: per_stream.get(s, 0) for s in STREAMS}})
    return rows


def _stream_summary(seqs: list[int], sizes: list[int]) -> dict[str, Any]:
    highest = max(seqs) if seqs else None
    present = set(seqs)
    missing = [] if highest is None else [n for n in range(highest + 1) if n not in present]
    return {
        "count": len(seqs),
        "highest_seq": highest,
        "missing": missing,
        "total_bytes": sum(sizes),
    }


def read_event_records(
    data_root: Path,
    study_id: int,
    session_id: uuid.UUID,
    seqs: list[int],
    max_inflated_bytes: int,
) -> list[EventRecord]:
    """Decode the stored events chunks of a session, in seq order. Raises OSError or ValueError."""
    chunks = [chunk_path(data_root, study_id, session_id, "events", n).read_bytes() for n in seqs]
    return parse_events(assemble_events(chunks, max_inflated_bytes=max_inflated_bytes))


def _read_events(
    data_root: Path,
    study_id: int,
    session_id: uuid.UUID,
    seqs: list[int],
    max_inflated_bytes: int,
) -> tuple[list[dict[str, Any]], int, str | None]:
    """Decode the stored events chunks, in seq order. Returns (shown, total, error)."""
    try:
        events = read_event_records(data_root, study_id, session_id, seqs, max_inflated_bytes)
    except (OSError, ValueError) as exc:
        return [], 0, str(exc)  # the rest of the detail is still worth showing
    shown = [e.model_dump(mode="json") for e in events[:MAX_EVENTS_SHOWN]]
    return shown, len(events), None


def session_detail(
    db: DbSession, data_root: Path, session_id: uuid.UUID, max_inflated_bytes: int
) -> dict[str, Any]:
    found = db.execute(
        select(Session, Study)
        .join(Study, Study.id == Session.study_id)
        .where(Session.id == session_id)
    ).one_or_none()
    if found is None:
        raise NotFoundError(f"session {session_id} not found")
    session, study = found

    per_stream: dict[str, tuple[list[int], list[int]]] = {s: ([], []) for s in STREAMS}
    rows = db.execute(
        select(Chunk.stream, Chunk.seq, Chunk.size)
        .where(Chunk.session_id == session_id)
        .order_by(Chunk.stream, Chunk.seq)
    )
    for stream, seq, size in rows:
        per_stream[stream.value][0].append(seq)
        per_stream[stream.value][1].append(size)
    streams = {name: _stream_summary(seqs, sizes) for name, (seqs, sizes) in per_stream.items()}

    events, total, error = _read_events(
        data_root, study.id, session_id, per_stream["events"][0], max_inflated_bytes
    )
    return {
        **_row(session, study),
        "consent_version": session.consent_version,
        "consent_at": session.consent_at.isoformat() if session.consent_at else None,
        "is_debug": session.is_debug,
        "client_info": session.client_info,
        "quality": session.quality,
        "quality_summary": quality_summary(session.quality),
        "stages": stage_rows(
            read_entries(derived_dir(data_root, study.id, session_id), session.quality)
        ),
        "plots": plots_on_disk(data_root, study.id, session_id),
        "jobs": job_rows(db, session_id),
        "rerun_refused": rerun_refusal(db, session),
        "replay": (derived_dir(data_root, study.id, session_id) / "debug" / REPLAY_FILE).is_file(),
        "render_refused": render_refusal(
            db,
            session,
            (derived_dir(data_root, study.id, session_id) / "gaze_a.parquet").is_file(),
        ),
        "webcam_available": session.raw_video_deleted_at is None
        and webcam_path(data_root, study.id, session_id).is_file(),
        "webcam_deleted_at": session.raw_video_deleted_at.isoformat()
        if session.raw_video_deleted_at
        else None,
        "chunks": {name: s["count"] for name, s in streams.items()},
        "streams": streams,
        "events": events,
        "events_total": total,
        "events_shown_max": MAX_EVENTS_SHOWN,
        "events_error": error,
    }


def stage_rows(entries: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Every stage in order with what its last run recorded (`app.stage_status`); a stage with no
    entry has not run."""
    rows = []
    for name in STAGE_NAMES:
        entry = entries.get(name)
        summary = None if entry is None else entry.get("summary")
        rows.append(
            {
                "name": name,
                "description": STAGE_DESCRIPTIONS[name],
                "ran": entry is not None,
                "status": None if entry is None else entry.get("status"),
                "seconds": None if entry is None else entry.get("seconds"),
                "skipped": summary["skipped"]
                if isinstance(summary, dict) and "skipped" in summary
                else None,
                "error": None if entry is None else entry.get("error"),
                "summary": summary,
            }
        )
    return rows


def quality_summary(quality: dict[str, Any] | None) -> dict[str, Any] | None:
    """The headline numbers of a quality report, ready to show; None without a report."""
    if quality is None:
        return None
    playback = quality.get("playback") or {}
    validation = quality.get("validation") or {}
    calibration = quality.get("calibration") or {}
    cv = calibration.get("cv") or {}
    events = quality.get("events") or {}
    lost = (
        (quality.get("face_lost") or {}).get("playback")
        or (quality.get("face_lost") or {}).get("all")
        or {}
    )
    sync = quality.get("sync") or {}
    return {
        "inclusion": inclusion_of(quality) or {"included": None, "reasons": [], "criteria": {}},
        "webcam": quality.get("webcam") or {},
        "valid_fraction": playback.get("valid_fraction"),
        "playback_frames": playback.get("frames"),
        "face_lost": lost,
        "fullscreen_exits": events.get("fullscreen_exits"),
        "stalls": events.get("stalls"),
        "calibration_status": calibration.get("status"),
        "calibration_failed_reason": calibration.get("failed_reason"),
        "cv_error_norm": cv.get("error_norm"),
        "cv_chosen": cv.get("chosen"),
        "validation_error_norm": validation.get("mean_error_norm"),
        "validation_error_corrected_norm": validation.get("mean_error_corrected_norm"),
        "validation_blocks": validation.get("blocks") or [],
        "sync_flags": sync.get("flags") or [],
        "sync_lag_ms": sync.get("lag_ms"),
        "sync_peak": sync.get("peak"),
        "gate": quality.get("browser_gate") or [],
        "plots": (quality.get("stages") or {}).get("plots", {}).get("summary") or {},
    }


def plots_on_disk(data_root: Path, study_id: int, session_id: uuid.UUID) -> list[dict[str, str]]:
    """The debug plots that exist for the session (only the known names, never a listing)."""
    directory = derived_dir(data_root, study_id, session_id) / "debug"
    return [
        {"name": name, "caption": caption}
        for name, caption in PLOT_CAPTIONS.items()
        if (directory / name).is_file()
    ]


def job_rows(db: DbSession, session_id: uuid.UUID) -> list[dict[str, Any]]:
    """The last processing jobs of a session, newest first; a failed one carries its log."""
    rows = []
    for job in jobs_of(db, session_id)[:5]:
        rows.append(
            {
                "id": job.id,
                "kind": job.kind.value,
                "status": job.status.value,
                "attempts": job.attempts,
                "from_stage": job.args.get("from_stage"),
                "only": job.args.get("only"),
                "created_at": job.created_at.isoformat(),
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
                "log": job.log[-3000:],
            }
        )
    return rows
