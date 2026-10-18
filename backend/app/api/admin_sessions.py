"""Admin sessions: read-only JSON API under `/admin/api/sessions` and HTML pages (PLAN §6)."""

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session as DbSession

from app import sessions_view
from app.api.pages import Page, check_form_csrf, page_admin, render
from app.auth import csrf_protected, current_admin, get_db
from app.chunks import MIB
from app.models import AdminUser
from app.models import Session as SessionRow
from app.pipeline_names import PLOT_FILES, REPLAY_FILE
from app.reprocess import queue_render, queue_rerun
from app.service_errors import NotFoundError
from app.settings import Settings
from app.storage import derived_dir, webcam_path
from app.studies import list_studies

log = logging.getLogger("admin")
router = APIRouter(prefix="/admin")


@router.get("/api/sessions")
def api_list(
    study_id: int | None = None,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return sessions_view.list_sessions(db, study_id)


@router.get("/api/sessions/{session_id}")
def api_get(
    request: Request,
    session_id: uuid.UUID,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    return sessions_view.session_detail(
        db, settings.data_root, session_id, settings.max_events_chunk_inflated_mb * MIB
    )


@router.get("/sessions")
def list_page(
    request: Request,
    study_id: Annotated[str, Query()] = "",  # the filter form sends "" for "all studies"
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    chosen = int(study_id) if study_id.isdigit() else None
    return render(
        request,
        page,
        "sessions.html",
        sessions=sessions_view.list_sessions(db, chosen),
        studies=list_studies(db),
        chosen_study=chosen,
    )


@router.get("/sessions/{session_id}")
def detail_page(
    request: Request,
    session_id: uuid.UUID,
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
    queued: Annotated[bool, Query()] = False,
) -> Response:
    settings: Settings = request.app.state.settings
    detail = sessions_view.session_detail(
        db, settings.data_root, session_id, settings.max_events_chunk_inflated_mb * MIB
    )
    return render(request, page, "session.html", session=detail, queued=queued)


class RerunRequest(BaseModel):
    """`POST /admin/api/sessions/{id}/rerun`: process again from a stage (None: from the start)."""

    model_config = ConfigDict(extra="forbid")
    from_stage: str | None = None


@router.post(
    "/api/sessions/{session_id}/rerun", status_code=201, dependencies=[Depends(csrf_protected)]
)
def api_rerun(
    session_id: uuid.UUID,
    body: RerunRequest,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> dict[str, Any]:
    job = queue_rerun(db, session_id, body.from_stage)
    db.commit()
    return {"job_id": job.id, "args": job.args}


@router.post("/sessions/{session_id}/rerun")
def rerun_form(
    session_id: uuid.UUID,
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
    csrf_token: Annotated[str, Form()] = "",
    from_stage: Annotated[str, Form()] = "",
) -> Response:
    check_form_csrf(page, csrf_token)
    queue_rerun(db, session_id, from_stage or None)
    db.commit()
    return RedirectResponse(f"/admin/sessions/{session_id}?queued=true", status_code=303)


def _session_row(db: DbSession, session_id: uuid.UUID) -> SessionRow:
    session = db.get(SessionRow, session_id)
    if session is None:
        raise NotFoundError(f"session {session_id} not found")
    return session


def _participant_video(path: Any, media_type: str, session_id: uuid.UUID, what: str) -> Response:
    """Serve a file that shows the participant (range requests work, so a video can seek). Logged,
    never cached."""
    log.info(
        "participant video served to an admin", extra={"session_id": str(session_id), "file": what}
    )
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


@router.get("/sessions/{session_id}/debug/{name}")
def debug_file(
    request: Request,
    session_id: uuid.UUID,
    name: str,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    """A debug plot or the replay video of the session; only the known file names, and only for a
    logged-in admin. The replay shows the participant's webcam video."""
    if name not in (*PLOT_FILES, REPLAY_FILE):
        raise NotFoundError(f"no debug file called {name}")
    session = _session_row(db, session_id)
    settings: Settings = request.app.state.settings
    path = derived_dir(settings.data_root, session.study_id, session_id) / "debug" / name
    if not path.is_file():
        raise NotFoundError(f"{name} has not been drawn for this session")
    if name == REPLAY_FILE:
        return _participant_video(path, "video/mp4", session_id, name)
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


def _render_args(request: Request, db: DbSession, session_id: uuid.UUID) -> bool:
    session = _session_row(db, session_id)
    settings: Settings = request.app.state.settings
    return (
        derived_dir(settings.data_root, session.study_id, session_id) / "gaze_a.parquet"
    ).is_file()


@router.post(
    "/api/sessions/{session_id}/render-debug",
    status_code=201,
    dependencies=[Depends(csrf_protected)],
)
def api_render_debug(
    request: Request,
    session_id: uuid.UUID,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> dict[str, Any]:
    job = queue_render(db, session_id, _render_args(request, db, session_id))
    db.commit()
    return {"job_id": job.id, "args": job.args}


@router.post("/sessions/{session_id}/render-debug")
def render_debug_form(
    request: Request,
    session_id: uuid.UUID,
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
    csrf_token: Annotated[str, Form()] = "",
) -> Response:
    check_form_csrf(page, csrf_token)
    queue_render(db, session_id, _render_args(request, db, session_id))
    db.commit()
    return RedirectResponse(f"/admin/sessions/{session_id}?queued=true", status_code=303)


def _webcam_file(request: Request, db: DbSession, session_id: uuid.UUID) -> Any:
    session = _session_row(db, session_id)
    if session.raw_video_deleted_at is not None:
        raise NotFoundError("the raw webcam video of this session has been deleted")
    settings: Settings = request.app.state.settings
    path = webcam_path(settings.data_root, session.study_id, session_id)
    if not path.is_file():
        raise NotFoundError(
            "the webcam recording has not been assembled (the session is not processed)"
        )
    return path


@router.get("/sessions/{session_id}/webcam")
def webcam_page(
    request: Request,
    session_id: uuid.UUID,
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    """The page that plays the participant's webcam recording, on request, with a warning."""
    _webcam_file(request, db, session_id)
    return render(request, page, "webcam.html", session_id=str(session_id))


@router.get("/sessions/{session_id}/webcam.webm")
def webcam_video(
    request: Request,
    session_id: uuid.UUID,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    return _participant_video(
        _webcam_file(request, db, session_id), "video/webm", session_id, "webcam.webm"
    )
