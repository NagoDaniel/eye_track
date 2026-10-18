"""Admin sessions: read-only JSON API under `/admin/api/sessions` and HTML pages (PLAN §6)."""

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
from app.pipeline_names import PLOT_FILES
from app.reprocess import queue_rerun
from app.service_errors import NotFoundError
from app.settings import Settings
from app.storage import derived_dir
from app.studies import list_studies

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


@router.get("/sessions/{session_id}/debug/{name}")
def debug_plot(
    request: Request,
    session_id: uuid.UUID,
    name: str,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    """A debug plot of the session; only the known file names, and only for a logged-in admin."""
    if name not in PLOT_FILES:
        raise NotFoundError(f"no plot called {name}")
    session = db.get(SessionRow, session_id)
    if session is None:
        raise NotFoundError(f"session {session_id} not found")
    settings: Settings = request.app.state.settings
    path = derived_dir(settings.data_root, session.study_id, session_id) / "debug" / name
    if not path.is_file():
        raise NotFoundError(f"{name} has not been drawn for this session")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})
