"""Chunk ingest and completeness (PLAN §6, §7). Stored chunks are never changed."""

import hashlib
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as Db

from app.events import assemble_events, parse_events
from app.models import Chunk, ChunkStream, SessionStatus
from app.models import Session as SessionRow
from app.schemas import ChunkAck, Stream
from app.storage import atomic_write_bytes, chunk_path, events_path

MIB = 1024 * 1024


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


def write_events_file(db: Db, root: Path, session: SessionRow) -> dict[str, Any] | None:
    """Assemble `raw/events.jsonl` from the events chunks in seq order.

    Returns `data.client_info` of the first `session_start` event, if there is one. Raises
    ValueError (naming the problem) if a chunk is not valid gzip or a line is not a valid event;
    nothing is written in that case.
    """
    seqs = db.scalars(
        select(Chunk.seq)
        .where(Chunk.session_id == session.id, Chunk.stream == ChunkStream.EVENTS)
        .order_by(Chunk.seq)
    ).all()
    chunks = [
        chunk_path(root, session.study_id, session.id, "events", seq).read_bytes() for seq in seqs
    ]
    jsonl = assemble_events(chunks)
    events = parse_events(jsonl)
    atomic_write_bytes(events_path(root, session.study_id, session.id), jsonl)
    for event in events:
        if event.type == "session_start":
            info = event.data.get("client_info")
            return info if isinstance(info, dict) else None
    return None
