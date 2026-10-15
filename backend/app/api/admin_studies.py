"""Admin studies: JSON API under `/admin/api/studies` and plain HTML pages (PLAN §4, §6).

The routes are thin; the logic is in `app.studies`.
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session as DbSession

from app import studies
from app.api.pages import Page, check_form_csrf, page_admin, render
from app.auth import csrf_protected, current_admin, get_db
from app.models import AdminUser
from app.service_errors import ConflictError, InvalidError

router = APIRouter(prefix="/admin")


class StudyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    code: str
    mode: str
    config: dict[str, Any]


def _out(db: DbSession, study_id: int) -> dict[str, Any]:
    study = studies.get_study(db, study_id)
    return studies.study_out(study, studies.session_counts(db).get(study.id, 0))


# --- JSON API ------------------------------------------------------------------------------------


@router.get("/api/studies")
def api_list(
    _admin: AdminUser = Depends(current_admin), db: DbSession = Depends(get_db)
) -> list[dict[str, Any]]:
    counts = studies.session_counts(db)
    return [studies.study_out(s, counts.get(s.id, 0)) for s in studies.list_studies(db)]


@router.post("/api/studies", status_code=201, dependencies=[Depends(csrf_protected)])
def api_create(body: StudyIn, db: DbSession = Depends(get_db)) -> dict[str, Any]:
    study = studies.create_study(db, body.name, body.code, body.mode, body.config)
    db.commit()
    return _out(db, study.id)


@router.get("/api/studies/{study_id}")
def api_get(
    study_id: int, _admin: AdminUser = Depends(current_admin), db: DbSession = Depends(get_db)
) -> dict[str, Any]:
    return _out(db, study_id)


@router.put("/api/studies/{study_id}", dependencies=[Depends(csrf_protected)])
def api_update(study_id: int, body: StudyIn, db: DbSession = Depends(get_db)) -> dict[str, Any]:
    studies.update_study(db, study_id, body.name, body.code, body.mode, body.config)
    db.commit()
    return _out(db, study_id)


@router.delete("/api/studies/{study_id}", status_code=204, dependencies=[Depends(csrf_protected)])
def api_delete(study_id: int, db: DbSession = Depends(get_db)) -> Response:
    studies.delete_study(db, study_id)
    db.commit()
    return Response(status_code=204)


# --- Pages ---------------------------------------------------------------------------------------


def _list_page(
    request: Request,
    page: Page,
    db: DbSession,
    form: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    counts = studies.session_counts(db)
    rows = [studies.study_out(s, counts.get(s.id, 0)) for s in studies.list_studies(db)]
    form = form or {
        "name": "",
        "code": "",
        "mode": "remote",
        "config": studies.default_config_json(),
    }
    return render(request, page, "studies.html", status_code, studies=rows, form=form, error=error)


def _detail_page(
    request: Request,
    page: Page,
    db: DbSession,
    study_id: int,
    form: dict[str, str] | None = None,
    error: str | None = None,
    saved: bool = False,
    status_code: int = 200,
) -> Response:
    study = _out(db, study_id)
    form = form or {
        "name": study["name"],
        "code": study["code"],
        "mode": study["mode"],
        "config": json.dumps(study["config"], indent=2),
    }
    # The api sees plain http from the proxy; Caddy says which scheme the browser used.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    link = f"{scheme}://{request.url.netloc}/s/{study['code']}"
    return render(
        request,
        page,
        "study.html",
        status_code,
        study=study,
        form=form,
        error=error,
        saved=saved,
        participant_link=link,
    )


@router.get("/studies")
def list_page(
    request: Request, page: Page = Depends(page_admin), db: DbSession = Depends(get_db)
) -> Response:
    return _list_page(request, page, db)


@router.post("/studies")
def create_form(
    request: Request,
    name: Annotated[str, Form()],
    code: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    config: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    check_form_csrf(page, csrf_token)
    try:
        study = studies.create_study(db, name, code, mode, config)
        db.commit()
    except (InvalidError, ConflictError) as exc:
        form = {"name": name, "code": code, "mode": mode, "config": config}
        return _list_page(request, page, db, form, str(exc), exc.status_code)
    return RedirectResponse(f"/admin/studies/{study.id}", status_code=303)


@router.get("/studies/{study_id}")
def detail_page(
    request: Request,
    study_id: int,
    saved: bool = False,
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    return _detail_page(request, page, db, study_id, saved=saved)


@router.post("/studies/{study_id}")
def update_form(
    request: Request,
    study_id: int,
    name: Annotated[str, Form()],
    code: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    config: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    check_form_csrf(page, csrf_token)
    try:
        studies.update_study(db, study_id, name, code, mode, config)
        db.commit()
    except (InvalidError, ConflictError) as exc:
        form = {"name": name, "code": code, "mode": mode, "config": config}
        return _detail_page(
            request, page, db, study_id, form=form, error=str(exc), status_code=exc.status_code
        )
    return RedirectResponse(f"/admin/studies/{study_id}?saved=true", status_code=303)


@router.post("/studies/{study_id}/delete")
def delete_form(
    request: Request,
    study_id: int,
    csrf_token: Annotated[str, Form()] = "",
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    check_form_csrf(page, csrf_token)
    try:
        studies.delete_study(db, study_id)
        db.commit()
    except ConflictError as exc:
        return _detail_page(
            request, page, db, study_id, error=str(exc), status_code=exc.status_code
        )
    return RedirectResponse("/admin/studies", status_code=303)
