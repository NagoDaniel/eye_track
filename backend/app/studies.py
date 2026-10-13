"""Study administration (PLAN §4, §4.1, §6): plain functions used by the JSON and form routes."""

import json
import re
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.consent import UnknownConsentVersion, consent_text
from app.models import Session, Study, StudyMode, Video
from app.schemas import StudyConfig
from app.service_errors import ConflictError, InvalidError, NotFoundError

STUDY_CODE_PATTERN = r"^[A-Za-z0-9_-]{3,32}$"
_STUDY_CODE_RE = re.compile(STUDY_CODE_PATTERN)


def default_config_json() -> str:
    return json.dumps(StudyConfig().model_dump(mode="json"), indent=2)


def parse_config(raw: object) -> dict[str, Any]:
    """Validate a config (a JSON string or an already decoded object); return it with defaults."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise InvalidError(f"config is not valid JSON: {exc}") from exc
    try:
        config = StudyConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}" for e in exc.errors()
        )
        raise InvalidError(f"invalid config: {problems}") from exc
    try:
        consent_text(config.consent_version)
    except UnknownConsentVersion as exc:
        raise InvalidError(f"invalid config: consent_version: {exc}") from exc
    return config.model_dump(mode="json")


def _check_fields(name: str, code: str, mode: str) -> tuple[str, str, StudyMode]:
    name = name.strip()
    if not name:
        raise InvalidError("name must not be empty")
    if not _STUDY_CODE_RE.fullmatch(code):
        raise InvalidError(f"code must match {STUDY_CODE_PATTERN}")
    try:
        return name, code, StudyMode(mode)
    except ValueError as exc:
        modes = ", ".join(m.value for m in StudyMode)
        raise InvalidError(f"mode must be one of: {modes}") from exc


def _check_playlist(db: DbSession, config: dict[str, Any]) -> None:
    for video_id in config["playlist"]:
        if not video_id.isascii() or not video_id.isdigit() or db.get(Video, int(video_id)) is None:
            raise InvalidError(f"playlist: video id {video_id!r} does not exist")


def _check_code_free(db: DbSession, code: str, own_id: int | None) -> None:
    other = db.scalar(select(Study.id).where(Study.code == code))
    if other is not None and other != own_id:
        raise ConflictError(f"study code {code!r} is already in use")


def get_study(db: DbSession, study_id: int) -> Study:
    study = db.get(Study, study_id)
    if study is None:
        raise NotFoundError(f"study {study_id} not found")
    return study


def list_studies(db: DbSession) -> list[Study]:
    return list(db.scalars(select(Study).order_by(Study.id)))


def session_counts(db: DbSession) -> dict[int, int]:
    rows = db.execute(select(Session.study_id, func.count()).group_by(Session.study_id))
    return {study_id: count for study_id, count in rows}


def _flush(db: DbSession, code: str) -> None:
    try:
        db.flush()
    except IntegrityError as exc:  # two requests with the same new code at the same time
        db.rollback()
        raise ConflictError(f"study code {code!r} is already in use") from exc


def create_study(db: DbSession, name: str, code: str, mode: str, config: object) -> Study:
    name, code, study_mode = _check_fields(name, code, mode)
    cfg = parse_config(config)
    _check_playlist(db, cfg)
    _check_code_free(db, code, None)
    study = Study(name=name, code=code, mode=study_mode, config=cfg)
    db.add(study)
    _flush(db, code)
    return study


def update_study(
    db: DbSession, study_id: int, name: str, code: str, mode: str, config: object
) -> Study:
    study = get_study(db, study_id)
    name, code, study_mode = _check_fields(name, code, mode)
    cfg = parse_config(config)
    _check_playlist(db, cfg)
    _check_code_free(db, code, study.id)
    study.name, study.code, study.mode, study.config = name, code, study_mode, cfg
    _flush(db, code)
    return study


def delete_study(db: DbSession, study_id: int) -> None:
    study = get_study(db, study_id)
    n = db.scalar(select(func.count()).select_from(Session).where(Session.study_id == study.id))
    if n:
        raise ConflictError(f"study {study_id} has {n} session(s) and cannot be deleted")
    db.delete(study)
    db.flush()


def study_out(study: Study, session_count: int) -> dict[str, Any]:
    return {
        "id": study.id,
        "name": study.name,
        "code": study.code,
        "mode": study.mode.value,
        "config": study.config,
        "session_count": session_count,
        "created_at": study.created_at.isoformat(),
    }
