"""Admin sessions: read-only JSON API under `/admin/api/sessions` and HTML pages (PLAN §6)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session as DbSession

from app import sessions_view
from app.api.pages import Page, page_admin, render
from app.auth import current_admin, get_db
from app.models import AdminUser
from app.settings import Settings
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
    return sessions_view.session_detail(db, settings.data_root, session_id)


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
) -> Response:
    settings: Settings = request.app.state.settings
    detail = sessions_view.session_detail(db, settings.data_root, session_id)
    return render(request, page, "session.html", session=detail)
