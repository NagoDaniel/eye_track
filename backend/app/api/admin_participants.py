"""Admin participant results: JSON API and HTML page (PLAN §6; DECISIONS 2026-10-14). Read-only."""

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session as DbSession

from app import participants_view
from app.api.pages import Page, page_admin, render
from app.auth import current_admin, get_db
from app.chunks import MIB
from app.models import AdminUser
from app.settings import Settings

router = APIRouter(prefix="/admin")


@router.get("/api/participants/{study_id}/{code}")
def api_get(
    request: Request,
    study_id: int,
    code: str,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    return participants_view.participant_results(
        db, settings.data_root, study_id, code, settings.max_events_chunk_inflated_mb * MIB
    )


@router.get("/participants/{study_id}/{code}")
def page(
    request: Request,
    study_id: int,
    code: str,
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    settings: Settings = request.app.state.settings
    results = participants_view.participant_results(
        db, settings.data_root, study_id, code, settings.max_events_chunk_inflated_mb * MIB
    )
    return render(request, page, "participant.html", results=results)
