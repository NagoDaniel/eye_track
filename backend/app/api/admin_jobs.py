"""Stopping a job from the admin (DECISIONS 2026-10-22): a queued job is cancelled at once, a
running one is asked to stop and the worker stops it between its steps."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from app.api.pages import Page, check_form_csrf, page_admin
from app.auth import csrf_protected, current_admin, get_db
from app.jobs import request_cancel
from app.models import AdminUser, Job
from app.service_errors import ConflictError, NotFoundError

router = APIRouter(prefix="/admin")


def _cancel(db: DbSession, job_id: int) -> str:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"job {job_id} not found")
    try:
        result = request_cancel(db, job)
    except ValueError as exc:
        raise ConflictError(str(exc)) from exc
    db.commit()
    return result


@router.post("/api/jobs/{job_id}/cancel", dependencies=[Depends(csrf_protected)])
def api_cancel(
    job_id: int,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> dict[str, Any]:
    return {"job_id": job_id, "result": _cancel(db, job_id)}


@router.post("/jobs/{job_id}/cancel")
def cancel_form(
    job_id: int,
    csrf_token: Annotated[str, Form()] = "",
    back: Annotated[str, Form()] = "/admin/studies",
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> RedirectResponse:
    check_form_csrf(page, csrf_token)
    _cancel(db, job_id)
    # only back to an admin page of this site
    target = back if back.startswith("/admin/") and "//" not in back else "/admin/studies"
    return RedirectResponse(target, status_code=303)
