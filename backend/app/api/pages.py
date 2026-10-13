"""Helpers shared by the server-rendered admin pages (no JavaScript: the proxy's CSP forbids it).

Pages redirect to the login form when nobody is logged in. State-changing forms carry a hidden
`csrf_token` field that is checked against the session, like the logout form.
"""

from dataclasses import dataclass
from typing import Any

from fastapi import Depends, HTTPException, Request, Response

from app.api.admin import templates
from app.auth import AdminSession, csrf_matches, optional_admin, read_session
from app.models import AdminUser

LOGIN_URL = "/admin/login"


@dataclass(frozen=True)
class Page:
    username: str
    session: AdminSession

    @property
    def csrf_token(self) -> str:
        return self.session.csrf


def page_admin(request: Request, admin: AdminUser | None = Depends(optional_admin)) -> Page:
    session = read_session(request)
    if admin is None or session is None:
        raise HTTPException(status_code=303, headers={"Location": LOGIN_URL})
    return Page(username=admin.username, session=session)


def check_form_csrf(page: Page, token: str) -> None:
    if not csrf_matches(page.session, token):
        raise HTTPException(status_code=403, detail="invalid CSRF token")


def render(
    request: Request, page: Page, template: str, status_code: int = 200, **context: Any
) -> Response:
    return templates.TemplateResponse(
        request,
        template,
        {"username": page.username, "csrf_token": page.csrf_token, **context},
        status_code=status_code,
    )
