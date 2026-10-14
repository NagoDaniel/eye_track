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
from app.schemas import EventRecord
from app.service_errors import NotFoundError
from app.storage import STREAMS, chunk_path

MAX_EVENTS_SHOWN = 1000


def _row(session: Session, study: Study) -> dict[str, Any]:
    return {
        "id": str(session.id),
        "study_id": study.id,
        "study_name": study.name,
        "study_code": study.code,
        "participant_code": session.participant_code,
        "status": session.status.value,
        "flags": session.flags,
        "created_at": session.created_at.isoformat(),
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
    data_root: Path, study_id: int, session_id: uuid.UUID, seqs: list[int]
) -> list[EventRecord]:
    """Decode the stored events chunks of a session, in seq order. Raises OSError or ValueError."""
    chunks = [chunk_path(data_root, study_id, session_id, "events", n).read_bytes() for n in seqs]
    return parse_events(assemble_events(chunks))


def _read_events(
    data_root: Path, study_id: int, session_id: uuid.UUID, seqs: list[int]
) -> tuple[list[dict[str, Any]], int, str | None]:
    """Decode the stored events chunks, in seq order. Returns (shown, total, error)."""
    try:
        events = read_event_records(data_root, study_id, session_id, seqs)
    except (OSError, ValueError) as exc:
        return [], 0, str(exc)  # the rest of the detail is still worth showing
    shown = [e.model_dump(mode="json") for e in events[:MAX_EVENTS_SHOWN]]
    return shown, len(events), None


def session_detail(db: DbSession, data_root: Path, session_id: uuid.UUID) -> dict[str, Any]:
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

    events, total, error = _read_events(data_root, study.id, session_id, per_stream["events"][0])
    return {
        **_row(session, study),
        "consent_version": session.consent_version,
        "consent_at": session.consent_at.isoformat() if session.consent_at else None,
        "is_debug": session.is_debug,
        "client_info": session.client_info,
        "quality": session.quality,
        "chunks": {name: s["count"] for name, s in streams.items()},
        "streams": streams,
        "events": events,
        "events_total": total,
        "events_shown_max": MAX_EVENTS_SHOWN,
        "events_error": error,
    }
