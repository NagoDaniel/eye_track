"""Admin login/logout, landing page and `/admin/api/*` JSON routes (PLAN §6)."""

import hashlib
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import (
    AdminSession,
    authenticate,
    clear_session,
    csrf_matches,
    current_admin,
    get_db,
    new_csrf_token,
    optional_admin,
    read_session,
    write_session,
)
from app.models import AdminUser
from app.settings import Settings

router = APIRouter(prefix="/admin")
ADMIN_UI = Path(__file__).resolve().parent.parent / "admin_ui"
templates = Jinja2Templates(directory=str(ADMIN_UI / "templates"))
# The editor script is cached by the browser; a changed file gets a new address.
templates.env.globals["aoi_editor_version"] = hashlib.sha256(
    (ADMIN_UI / "static" / "aoi_editor.js").read_bytes()
).hexdigest()[:12]


def login_page(request: Request, status_code: int = 200, error: str | None = None) -> Response:
    settings: Settings = request.app.state.settings
    session = read_session(request)
    if session is None or session.admin_id is not None:
        session = AdminSession(csrf=new_csrf_token())
    response = templates.TemplateResponse(
        request, "login.html", {"csrf_token": session.csrf, "error": error}, status_code=status_code
    )
    write_session(response, settings, session)
    return response


@router.get("/login")
def login_form(request: Request) -> Response:
    return login_page(request)


@router.post("/login")
def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
) -> Response:
    settings: Settings = request.app.state.settings
    if not csrf_matches(read_session(request), csrf_token):
        raise HTTPException(status_code=403, detail="invalid CSRF token")
    user = authenticate(db, username, password)
    if user is None:
        return login_page(request, status_code=401, error="Invalid username or password.")
    response = RedirectResponse("/admin/", status_code=303)
    # Fresh CSRF token on privilege change.
    write_session(response, settings, AdminSession(csrf=new_csrf_token(), admin_id=user.id))
    return response


@router.post("/logout")
async def logout(request: Request) -> Response:
    form = await request.form()  # read by hand: an empty body must give 403, not 422
    if not csrf_matches(read_session(request), str(form.get("csrf_token", ""))):
        raise HTTPException(status_code=403, detail="invalid CSRF token")
    response = RedirectResponse("/admin/login", status_code=303)
    clear_session(response, request.app.state.settings)
    return response


@router.get("/")
def landing(request: Request, admin: AdminUser | None = Depends(optional_admin)) -> Response:
    session = read_session(request)
    if admin is None or session is None:
        return RedirectResponse("/admin/login", status_code=303)
    return templates.TemplateResponse(
        request, "index.html", {"username": admin.username, "csrf_token": session.csrf}
    )


@router.get("/api/me")
def me(admin: AdminUser = Depends(current_admin)) -> dict[str, str]:
    return {"username": admin.username}
