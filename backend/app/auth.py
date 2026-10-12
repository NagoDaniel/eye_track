"""Admin authentication (PLAN §6, §10): argon2 passwords, signed session cookie, CSRF.

The session is a signed, time-limited cookie holding the admin id and a CSRF token. It is
stateless, so logging out clears the cookie but cannot revoke a copy of it before it expires.
"""

import hmac
import secrets
from collections.abc import Generator
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Header, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AdminUser
from app.settings import Settings

COOKIE_NAME = "eyetrack_admin"
_hasher = PasswordHasher()  # argon2id with the library's current defaults
_DUMMY_HASH = _hasher.hash("dummy-password-for-constant-time-checks")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def create_admin(db: Session, username: str, password: str, min_password_length: int) -> AdminUser:
    """Add an admin account. Raises ValueError with a clear message on bad input."""
    username = username.strip()
    if not username:
        raise ValueError("username must not be empty")
    if len(password) < min_password_length:
        raise ValueError(f"password must be at least {min_password_length} characters")
    if db.scalar(select(AdminUser.id).where(AdminUser.username == username)) is not None:
        raise ValueError(f"admin '{username}' already exists")
    user = AdminUser(username=username, password_hash=hash_password(password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise ValueError(f"admin '{username}' already exists") from exc
    return user


def authenticate(db: Session, username: str, password: str) -> AdminUser | None:
    user = db.scalar(select(AdminUser).where(AdminUser.username == username.strip()))
    # Hash against a dummy when the user is unknown so timing does not reveal valid usernames.
    ok = verify_password(user.password_hash if user else _DUMMY_HASH, password)
    return user if (user is not None and ok) else None


@dataclass(frozen=True)
class AdminSession:
    csrf: str
    admin_id: int | None = None


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="admin-session")


def read_session(request: Request) -> AdminSession | None:
    settings: Settings = request.app.state.settings
    raw = request.cookies.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        data = _serializer(settings).loads(raw, max_age=settings.admin_session_max_age_s)
        return AdminSession(csrf=str(data["csrf"]), admin_id=data.get("admin_id"))
    except (BadSignature, KeyError, TypeError):
        return None


def write_session(response: Response, settings: Settings, session: AdminSession) -> None:
    payload = {"csrf": session.csrf, "admin_id": session.admin_id}
    response.set_cookie(
        COOKIE_NAME,
        _serializer(settings).dumps(payload),
        max_age=settings.admin_session_max_age_s,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="strict",
        path="/",
    )


def clear_session(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        httponly=True,
        secure=settings.admin_cookie_secure,
        samesite="strict",
        path="/",
    )


def csrf_matches(session: AdminSession | None, token: str | None) -> bool:
    return bool(session and token and hmac.compare_digest(session.csrf, token))


def get_db(request: Request) -> Generator[Session]:
    with request.app.state.session_factory() as db:
        yield db


def optional_admin(request: Request, db: Session = Depends(get_db)) -> AdminUser | None:
    session = read_session(request)
    if session is None or session.admin_id is None:
        return None
    return db.get(AdminUser, session.admin_id)


def current_admin(admin: AdminUser | None = Depends(optional_admin)) -> AdminUser:
    if admin is None:
        raise HTTPException(status_code=401, detail="login required")
    return admin


def csrf_protected(
    request: Request,
    _admin: AdminUser = Depends(current_admin),
    x_csrf_token: str | None = Header(default=None),
) -> None:
    """Dependency for state-changing JSON admin routes: requires login and `X-CSRF-Token`."""
    if not csrf_matches(read_session(request), x_csrf_token):
        raise HTTPException(status_code=403, detail="invalid CSRF token")
