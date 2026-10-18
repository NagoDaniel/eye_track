"""Participant API (PLAN §6): study info, session creation, consent, chunk upload, completion.

Session endpoints authenticate with `Authorization: Bearer <upload_token>`; the token is shown once
at session creation and only its sha256 is stored.
"""

import hashlib
import hmac
import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Request, Response
from fastapi.concurrency import run_in_threadpool
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session as Db

from app.auth import get_db, optional_admin, release_connection
from app.chunks import (
    EBML_MAGIC,
    MIB,
    ChunkConflict,
    SessionTotalExceeded,
    missing_chunks,
    sha256_hex,
    store_chunk,
    write_events_file,
    write_features_file,
    write_stimulus_frames_file,
)
from app.consent import UnknownConsentVersion, consent_text
from app.events import gunzip_limited
from app.jobs import enqueue
from app.models import AdminUser, Chunk, JobKind, SessionStatus, Study, Video
from app.models import Session as SessionRow
from app.schemas import (
    BROWSER_REQUIREMENTS,
    ChunkAck,
    ClientErrorRequest,
    CompleteRequest,
    CompleteResponse,
    ConsentRequest,
    ConsentResponse,
    SessionCreateRequest,
    SessionCreateResponse,
    Stream,
    StudyConfig,
    StudyInfo,
    VideoRef,
)
from app.session_meta import read_config, write_meta
from app.settings import Settings
from app.storage import raw_dir

log = logging.getLogger("app.participant")

router = APIRouter(prefix="/api")

# Streams whose chunks are gzip-compressed JSON lines.
JSONL_STREAMS: tuple[Stream, ...] = ("events", "features", "stimulus_frames")

# `chunks.seq` is a 32-bit integer column.
MAX_SEQ = 2**31 - 1


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def client_ip(request: Request) -> str:
    """The rightmost `X-Forwarded-For` entry (appended by the Caddy proxy in front of the api,
    which is not reachable otherwise), else the socket peer. Earlier entries are client-supplied."""
    forwarded = request.headers.get("x-forwarded-for", "")
    last = forwarded.rsplit(",", 1)[-1].strip()
    if last:
        return last
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(request: Request, bucket: str = "limiter") -> None:
    if not getattr(request.app.state, bucket).allow(client_ip(request)):
        raise HTTPException(status_code=429, detail="too many requests, try again later")


def authed_session(
    session_id: str,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    db: Db = Depends(get_db),
) -> SessionRow:
    """The session named in the path, if the bearer token is its (unexpired) upload token."""
    settings: Settings = request.app.state.settings
    unauthorized = HTTPException(
        status_code=401,
        detail="invalid or missing upload token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized
    try:
        sid = uuid.UUID(session_id)
    except ValueError:
        raise unauthorized from None
    session = db.get(SessionRow, sid)
    if session is None or not hmac.compare_digest(session.upload_token_hash, hash_token(token)):
        raise unauthorized
    age_s = (datetime.now(UTC) - session.created_at).total_seconds()
    if age_s > settings.upload_token_max_age_s:
        raise unauthorized
    release_connection(db)  # before the body of a chunk upload is read, see release_connection
    return session


def study_config(study: Study) -> StudyConfig:
    return StudyConfig.model_validate(study.config)


@router.get("/studies/{code}")
def study_info(code: str, db: Db = Depends(get_db)) -> StudyInfo:
    study = db.scalar(select(Study).where(Study.code == code))
    if study is None:
        raise HTTPException(status_code=404, detail="unknown study")
    config = study_config(study)
    try:
        text = consent_text(config.consent_version)
    except UnknownConsentVersion as exc:
        log.error("study has no consent text", extra={"study_code": code, "error": str(exc)})
        raise HTTPException(status_code=500, detail="consent text unavailable") from exc
    return StudyInfo(
        name=study.name,
        code=study.code,
        mode=study.mode.value,
        consent_version=config.consent_version,
        consent_text=text,
        browser_requirements=list(BROWSER_REQUIREMENTS),
    )


@router.post("/sessions", status_code=201)
def create_session(
    body: SessionCreateRequest,
    request: Request,
    db: Db = Depends(get_db),
    admin: AdminUser | None = Depends(optional_admin),
) -> SessionCreateResponse:
    enforce_rate_limit(request)
    settings: Settings = request.app.state.settings
    study = db.scalar(select(Study).where(Study.code == body.study_code))
    if study is None:
        raise HTTPException(status_code=404, detail="unknown study")
    config = study_config(study)
    # PLAN §12.1: debug mode needs `debug_allowed` or an admin logged in on this browser.
    if body.debug and not (config.debug_allowed or admin is not None):
        raise HTTPException(status_code=403, detail="debug mode is not allowed for this study")

    videos: list[VideoRef] = []
    for video_id in config.playlist:
        video = db.get(Video, int(video_id)) if video_id.isascii() and video_id.isdigit() else None
        if video is None:
            log.error(
                "playlist references a video that does not exist",
                extra={"study_code": study.code, "video_id": video_id},
            )
            raise HTTPException(
                status_code=500, detail="the study is not set up correctly"
            ) from None
        videos.append(
            VideoRef(
                id=str(video.id),
                url=f"/media/videos/{video.id}.mp4",
                sha256=video.sha256,
                duration=video.duration,
                fps=video.fps,
            )
        )

    token = secrets.token_urlsafe(32)
    session = SessionRow(
        study_id=study.id,
        participant_code=body.participant_code,
        upload_token_hash=hash_token(token),
        status=SessionStatus.CREATED,
        is_debug=body.debug,
        flags=[],
    )
    db.add(session)
    db.flush()  # assigns id and created_at
    raw_dir(settings.data_root, session.study_id, session.id).mkdir(parents=True, exist_ok=True)
    write_meta(settings.data_root, session, config.model_dump(mode="json"))
    db.commit()
    return SessionCreateResponse(
        session_id=str(session.id), upload_token=token, config=config, videos=videos
    )


@router.post("/sessions/{session_id}/consent")
def consent(
    body: ConsentRequest,
    session: SessionRow = Depends(authed_session),
    db: Db = Depends(get_db),
) -> ConsentResponse:
    study = db.get(Study, session.study_id)
    if study is None:
        raise HTTPException(status_code=500, detail="study of this session is missing")
    expected = study_config(study).consent_version
    if body.consent_version != expected:
        raise HTTPException(
            status_code=409,
            detail=f"consent version {body.consent_version!r} is not the study's {expected!r}",
        )
    if session.status in (SessionStatus.CONSENTED, SessionStatus.RECORDING):
        return ConsentResponse(status="consented")
    if session.status != SessionStatus.CREATED:
        raise HTTPException(status_code=409, detail=f"session is {session.status.value}")
    session.status = SessionStatus.CONSENTED
    session.consent_version = body.consent_version
    session.consent_at = datetime.now(UTC)
    db.commit()
    return ConsentResponse(status="consented")


async def read_body(request: Request, limit: int) -> bytes:
    """The raw request body, or 413 as soon as it is larger than `limit` bytes."""
    too_big = HTTPException(status_code=413, detail=f"body larger than {limit} bytes")
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise too_big
    body = bytearray()
    async for part in request.stream():
        body += part
        if len(body) > limit:
            raise too_big
    return bytes(body)


@router.put("/sessions/{session_id}/chunks/{stream}/{seq}")
async def put_chunk(
    stream: Stream,
    seq: Annotated[int, Path(ge=0, le=MAX_SEQ)],
    request: Request,
    x_chunk_sha256: Annotated[str, Header()],
    session: SessionRow = Depends(authed_session),
    db: Db = Depends(get_db),
) -> ChunkAck:
    settings: Settings = request.app.state.settings
    if session.status == SessionStatus.CREATED:
        raise HTTPException(status_code=409, detail="consent missing: record consent first")
    if session.status not in (SessionStatus.CONSENTED, SessionStatus.RECORDING):
        done = session.status == SessionStatus.UPLOADED
        detail = "session already completed" if done else f"session is {session.status.value}"
        raise HTTPException(status_code=409, detail=detail)
    body = await read_body(request, settings.max_upload_chunk_mb * MIB)
    if not body:
        raise HTTPException(status_code=400, detail="empty chunk")
    if sha256_hex(body) != x_chunk_sha256:
        raise HTTPException(status_code=400, detail="X-Chunk-SHA256 does not match the body")
    if stream in JSONL_STREAMS:
        # Only gzip validity and size are checked here, so a corrupt or bomb chunk is never
        # stored; the lines are not (an unknown event type must not block the upload, see
        # /complete).
        try:
            await run_in_threadpool(
                gunzip_limited, body, settings.max_events_chunk_inflated_mb * MIB
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail=f"{stream} chunk is not valid gzip: {exc}"
            ) from exc
    elif stream == "webcam" and seq == 0 and not body.startswith(EBML_MAGIC):
        raise HTTPException(status_code=422, detail="the first webcam chunk is not a WebM file")
    try:
        return await run_in_threadpool(
            store_chunk,
            db,
            settings.data_root,
            session,
            stream,
            seq,
            body,
            x_chunk_sha256,
            settings.max_session_total_mb * MIB,
        )
    except ChunkConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SessionTotalExceeded as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/complete")
def complete(
    body: CompleteRequest,
    request: Request,
    session: SessionRow = Depends(authed_session),
    db: Db = Depends(get_db),
) -> CompleteResponse:
    settings: Settings = request.app.state.settings
    if session.status not in (
        SessionStatus.CONSENTED,
        SessionStatus.RECORDING,
        SessionStatus.UPLOADED,
    ):
        raise HTTPException(status_code=409, detail=f"session already {session.status.value}")
    too_many = [s for s, last in body.last_seq.items() if last >= settings.max_chunks_per_stream]
    if too_many:
        raise HTTPException(
            status_code=422,
            detail=f"last_seq of {too_many} exceeds {settings.max_chunks_per_stream} chunks",
        )
    missing = missing_chunks(db, session.id, dict(body.last_seq))
    if missing:
        return CompleteResponse(status="incomplete", missing=missing)

    root = settings.data_root
    max_inflated = settings.max_events_chunk_inflated_mb * MIB
    events = write_events_file(db, root, session, max_inflated)
    problems = {
        "events_invalid": events.error,
        "features_invalid": write_features_file(db, root, session, max_inflated),
        "stimulus_frames_invalid": write_stimulus_frames_file(db, root, session, max_inflated),
    }
    for flag, error in problems.items():
        if error is not None:
            # Stored chunks never change, so the participant could never fix this: finish the
            # session, flag it and let the researcher decide (PLAN §1: flag the session, move on).
            log.error(
                "stream cannot be assembled",
                extra={"session_id": str(session.id), "flag": flag, "error": error},
            )
    try:
        config = read_config(root, session)
    except (OSError, ValueError, KeyError) as exc:
        log.error(
            "session metadata is unreadable",
            extra={"session_id": str(session.id), "error": str(exc)},
        )
        raise HTTPException(status_code=500, detail="session metadata is unreadable") from exc
    if events.client_info is not None:
        session.client_info = events.client_info
    no_chunks = not db.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.session_id == session.id)
    )
    for flag, wanted in (
        ("interrupted", body.interrupted),
        *((flag, error is not None) for flag, error in problems.items()),
        ("calibration_gate_failed", events.gate_failed),
        ("empty", no_chunks),
    ):
        if wanted and flag not in session.flags:
            session.flags = [*session.flags, flag]
    first_completion = session.status != SessionStatus.UPLOADED
    session.status = SessionStatus.UPLOADED
    write_meta(root, session, config)
    if first_completion and not no_chunks:
        # Once per session (a repeated /complete must not queue it again); a session without any
        # chunk has nothing to process.
        enqueue(db, JobKind.PROCESS_SESSION, {"session_id": str(session.id)})
    db.commit()
    return CompleteResponse(status="uploaded", missing={})


@router.post("/client-errors", status_code=204)
async def client_error(request: Request) -> Response:
    """Browser error report: no authentication, structured log only (DECISIONS 1b item 10)."""
    enforce_rate_limit(request, "error_limiter")
    settings: Settings = request.app.state.settings
    raw = await read_body(request, settings.client_error_max_bytes)
    try:
        report = ClientErrorRequest.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=exc.errors(include_input=False, include_context=False)
        ) from exc
    # `message` is reserved by logging's LogRecord, hence `error_message`. Nothing else from the
    # request (headers, IP, cookies) is logged.
    log.warning(
        "client error",
        extra={
            "error_message": report.message,
            "stack": report.stack,
            "url": report.url,
            "t": report.t,
            "session_id": report.session_id,
        },
    )
    return Response(status_code=204)
