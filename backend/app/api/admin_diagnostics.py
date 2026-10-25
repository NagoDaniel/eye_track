"""The diagnostics page of the admin (PLAN §12.3)."""

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session as DbSession

from app.api.pages import Page, page_admin, render
from app.auth import current_admin, get_db
from app.diagnostics import diagnostics
from app.models import AdminUser
from app.settings import Settings

router = APIRouter(prefix="/admin")


def _report(request: Request, db: DbSession) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    return diagnostics(db, settings.data_root, settings.worker_heartbeat_max_age_s)


@router.get("/diagnostics")
def diagnostics_page(
    request: Request, page: Page = Depends(page_admin), db: DbSession = Depends(get_db)
) -> Response:
    return render(request, page, "diagnostics.html", report=_report(request, db))


@router.get("/api/diagnostics")
def diagnostics_api(
    request: Request, _admin: AdminUser = Depends(current_admin), db: DbSession = Depends(get_db)
) -> dict[str, Any]:
    report = _report(request, db)
    report["disk"] = asdict(report["disk"])
    return report
