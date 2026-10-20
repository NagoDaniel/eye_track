"""Results of one participant in one study, for the admin (PLAN §6; DECISIONS 2026-10-14).

Later milestones add their per-participant results (quality, plots, heatmaps, AOI metrics) to this
view instead of creating new pages.
"""

import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.analysis_view import participant_analysis
from app.models import Chunk, ChunkStream
from app.schemas import EventRecord
from app.sessions_view import list_sessions, read_event_records
from app.studies import get_study

MS_PER_S = 1000.0


def summarize_events(events: list[EventRecord]) -> dict[str, Any]:
    """Counts and times from one session's events. Pure; never reads a file."""
    dots = {phase: {"shown": 0, "clicked": 0} for phase in ("calibration", "validation")}
    videos: dict[str, dict[str, Any]] = {}
    first_play: dict[str, float] = {}
    fullscreen_exits = 0
    client_errors = 0

    def video(video_id: str) -> dict[str, Any]:
        return videos.setdefault(
            video_id,
            {
                "video_id": video_id,
                "loaded": False,
                "played": False,
                "ended": False,
                "watch_s": None,
                "pauses": 0,
                "stalls": 0,
            },
        )

    for e in events:
        phase = e.data.get("phase")
        if e.type == "calib_dot_show" and phase in dots:
            dots[phase]["shown"] += 1
        elif e.type == "calib_dot_click" and phase in dots:
            dots[phase]["clicked"] += 1
        elif e.type == "fullscreen_change" and e.data.get("fullscreen") is False:
            fullscreen_exits += 1
        elif e.type == "client_error":
            client_errors += 1
        elif e.type.startswith("video_") and "video_id" in e.data:
            v = video(str(e.data["video_id"]))
            if e.type == "video_load":
                v["loaded"] = True
            elif e.type == "video_play":
                v["played"] = True
                first_play.setdefault(v["video_id"], e.t)
            elif e.type == "video_pause":
                v["pauses"] += 1
            elif e.type == "video_stall":
                v["stalls"] += 1
            elif e.type == "video_ended":
                v["ended"] = True
                if v["video_id"] in first_play:
                    v["watch_s"] = (e.t - first_play[v["video_id"]]) / MS_PER_S

    duration = (events[-1].t - events[0].t) / MS_PER_S if events else None
    return {
        "dots": dots,
        "videos": list(videos.values()),
        "fullscreen_exits": fullscreen_exits,
        "client_errors": client_errors,
        "duration_s": duration,
    }


def participant_results(
    db: DbSession, data_root: Path, study_id: int, participant_code: str, max_inflated_bytes: int
) -> dict[str, Any]:
    """All sessions of a participant code in a study, each with its event summary.

    A session whose events cannot be read still appears, with `events_error` and no summary;
    missing event chunks are listed, never hidden.
    """
    study = get_study(db, study_id)
    code = participant_code.strip().upper()
    sessions = []
    for row in list_sessions(db, study_id):
        if row["participant_code"] != code:
            continue
        seqs = list(
            db.scalars(
                select(Chunk.seq)
                .where(Chunk.session_id == row["id"], Chunk.stream == ChunkStream.EVENTS)
                .order_by(Chunk.seq)
            )
        )
        present = set(seqs)
        missing = [n for n in range(max(seqs) + 1) if n not in present] if seqs else []
        summary: dict[str, Any] | None = None
        error: str | None = None
        try:
            events = read_event_records(
                data_root, study.id, uuid.UUID(row["id"]), seqs, max_inflated_bytes
            )
            summary = summarize_events(events)
        except (OSError, ValueError) as exc:
            error = str(exc)
        sessions.append(
            {**row, "summary": summary, "events_error": error, "missing_event_chunks": missing}
        )
    return {
        "study": {"id": study.id, "name": study.name, "code": study.code},
        "participant_code": code,
        "sessions": sessions,
        "analysis": participant_analysis(db, data_root, study_id, code),
    }
