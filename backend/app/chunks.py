"""Chunk ingest and completeness (PLAN §6, §7). Stored chunks are never changed."""

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as Db

from app.events import assemble_events, calibration_gate_failed, parse_events, parse_records
from app.models import Chunk, ChunkStream, SessionStatus
from app.models import Session as SessionRow
from app.schemas import ChunkAck, FeatureRecord, StimulusFrameRecord, Stream
from app.storage import (
    atomic_write_bytes,
    browser_features_path,
    chunk_path,
    events_path,
    stimulus_frames_path,
)

MIB = 1024 * 1024

# Every WebM (EBML) file starts with these four bytes; only chunk 0 of the webcam stream has them
# (PLAN §5.4).
EBML_MAGIC = b"\x1a\x45\xdf\xa3"


class ChunkConflict(Exception):
    """The same (session, stream, seq) is already stored with different bytes."""


class SessionTotalExceeded(Exception):
    """Storing this chunk would take the session over `max_session_total_mb`."""


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ack_if_same(existing: Chunk, digest: str, stream: Stream, seq: int) -> ChunkAck:
    if existing.sha256 != digest:
        raise ChunkConflict(f"{stream} chunk {seq} is already stored with different content")
    return ChunkAck(stream=stream, seq=seq, size=existing.size)


def store_chunk(
    db: Db,
    root: Path,
    session: SessionRow,
    stream: Stream,
    seq: int,
    body: bytes,
    digest: str,
    max_total_bytes: int,
) -> ChunkAck:
    """Store one chunk (idempotent). The caller has checked auth, session status and the hash."""
    key = (session.id, ChunkStream(stream), seq)
    existing = db.get(Chunk, key)
    if existing is not None:
        return _ack_if_same(existing, digest, stream, seq)

    total = db.scalar(
        select(func.coalesce(func.sum(Chunk.size), 0)).where(Chunk.session_id == session.id)
    )
    if (total or 0) + len(body) > max_total_bytes:
        raise SessionTotalExceeded(f"session would exceed {max_total_bytes} bytes")

    db.add(Chunk(session_id=session.id, stream=key[1], seq=seq, size=len(body), sha256=digest))
    if session.status == SessionStatus.CONSENTED:
        session.status = SessionStatus.RECORDING
    try:
        # The insert comes first: two concurrent PUTs of the same chunk race here, and only the
        # winner goes on to write the file, so a loser can never overwrite the winner's bytes.
        db.flush()
    except IntegrityError:
        db.rollback()
        winner = db.get(Chunk, key)
        if winner is None:
            raise
        return _ack_if_same(winner, digest, stream, seq)
    atomic_write_bytes(chunk_path(root, session.study_id, session.id, stream, seq), body)
    db.commit()
    return ChunkAck(stream=stream, seq=seq, size=len(body))


def missing_chunks(
    db: Db, session_id: uuid.UUID, last_seq: dict[Stream, int]
) -> dict[Stream, list[int]]:
    """For each stream, every seq in 0..last_seq that has no row in `chunks`."""
    missing: dict[Stream, list[int]] = {}
    for stream, last in last_seq.items():
        have = set(
            db.scalars(
                select(Chunk.seq).where(
                    Chunk.session_id == session_id, Chunk.stream == ChunkStream(stream)
                )
            )
        )
        gaps = [seq for seq in range(last + 1) if seq not in have]
        if gaps:
            missing[stream] = gaps
    return missing


@dataclass(frozen=True)
class EventsResult:
    client_info: dict[str, Any] | None = None
    gate_failed: bool = False
    error: str | None = None


def _stored_chunks(db: Db, root: Path, session: SessionRow, stream: Stream) -> list[bytes]:
    seqs = db.scalars(
        select(Chunk.seq)
        .where(Chunk.session_id == session.id, Chunk.stream == ChunkStream(stream))
        .order_by(Chunk.seq)
    ).all()
    return [
        chunk_path(root, session.study_id, session.id, stream, seq).read_bytes() for seq in seqs
    ]


def write_events_file(
    db: Db, root: Path, session: SessionRow, max_inflated_bytes: int
) -> EventsResult:
    """Assemble `raw/events.jsonl` from the events chunks in seq order.

    The result holds `data.client_info` of the first `session_start` event if there is one, and
    whether the last pre-video calibration gate failed (PLAN §5.6). If a chunk is not valid gzip or
    a line is not a valid event (for example an event type this server does not know, or a
    `calib_gate_result` of the wrong shape), nothing is written and the problem is returned as
    `error`: the session still completes and the caller flags it (DECISIONS, 1b review).
    """
    chunks = _stored_chunks(db, root, session, "events")
    try:
        jsonl = assemble_events(chunks, max_inflated_bytes=max_inflated_bytes)
        events = parse_events(jsonl)
        gate_failed = calibration_gate_failed(events)
    except ValueError as exc:
        return EventsResult(error=str(exc))
    atomic_write_bytes(events_path(root, session.study_id, session.id), jsonl)
    for event in events:
        if event.type == "session_start":
            info = event.data.get("client_info")
            return EventsResult(
                client_info=info if isinstance(info, dict) else None, gate_failed=gate_failed
            )
    return EventsResult(gate_failed=gate_failed)


def write_features_file(
    db: Db, root: Path, session: SessionRow, max_inflated_bytes: int
) -> str | None:
    """Assemble `raw/browser_features.jsonl`; nothing is written when there are no chunks.

    Returns None, or the problem if a chunk or a line is invalid (then nothing is written).
    """
    return _write_records(
        db,
        root,
        session,
        "features",
        browser_features_path(root, session.study_id, session.id),
        FeatureRecord,
        max_inflated_bytes,
    )


def write_stimulus_frames_file(
    db: Db, root: Path, session: SessionRow, max_inflated_bytes: int
) -> str | None:
    """Assemble `raw/stimulus_frames.jsonl`; same rules as `write_features_file`."""
    return _write_records(
        db,
        root,
        session,
        "stimulus_frames",
        stimulus_frames_path(root, session.study_id, session.id),
        StimulusFrameRecord,
        max_inflated_bytes,
    )


def _write_records(
    db: Db,
    root: Path,
    session: SessionRow,
    stream: Stream,
    path: Path,
    model: type[FeatureRecord] | type[StimulusFrameRecord],
    max_inflated_bytes: int,
) -> str | None:
    chunks = _stored_chunks(db, root, session, stream)
    if not chunks:
        return None
    try:
        jsonl = assemble_events(chunks, max_inflated_bytes=max_inflated_bytes, name=stream)
        parse_records(jsonl, model, stream)
    except ValueError as exc:
        return str(exc)
    atomic_write_bytes(path, jsonl)
    return None
