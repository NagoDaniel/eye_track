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

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as Db

from app.auth import get_db
from app.consent import UnknownConsentVersion, consent_text
from app.models import Session as SessionRow
from app.models import SessionStatus, Study, Video
from app.schemas import (
    BROWSER_REQUIREMENTS,
    ConsentRequest,
    ConsentResponse,
    SessionCreateRequest,
    SessionCreateResponse,
    StudyConfig,
    StudyInfo,
    VideoRef,
)
from app.session_meta import write_meta
from app.settings import Settings
from app.storage import raw_dir

log = logging.getLogger("app.participant")

router = APIRouter(prefix="/api")


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


def enforce_rate_limit(request: Request) -> None:
    if not request.app.state.limiter.allow(client_ip(request)):
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
    body: SessionCreateRequest, request: Request, db: Db = Depends(get_db)
) -> SessionCreateResponse:
    enforce_rate_limit(request)
    settings: Settings = request.app.state.settings
    study = db.scalar(select(Study).where(Study.code == body.study_code))
    if study is None:
        raise HTTPException(status_code=404, detail="unknown study")
    config = study_config(study)

    videos: list[VideoRef] = []
    for video_id in config.playlist:
        video = db.get(Video, int(video_id)) if video_id.isdigit() else None
        if video is None:
            log.error(
                "playlist references a video that does not exist",
                extra={"study_code": study.code, "video_id": video_id},
            )
            raise HTTPException(status_code=500, detail=f"playlist video {video_id} is missing")
        videos.append(
            VideoRef(
                id=str(video.id),
                url=f"/media/videos/{video.id}.mp4",
                sha256=video.sha256,
                duration=video.duration,
            )
        )

    token = secrets.token_urlsafe(32)
    session = SessionRow(
        study_id=study.id,
        participant_code=body.participant_code,
        upload_token_hash=hash_token(token),
        status=SessionStatus.CREATED,
        is_debug=False,
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
