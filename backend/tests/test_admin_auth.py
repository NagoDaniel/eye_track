import re
import time
from collections.abc import Generator

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session as DbSession

from app.auth import create_admin, hash_password, verify_password
from app.main import create_app
from app.models import AdminUser
from tests.dbutil import make_settings

USER, PASSWORD = "alice", "correct horse battery"


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, html
    return m.group(1)


@pytest.fixture
def engine_with_admin(migrated_engine: Engine) -> Engine:
    with DbSession(migrated_engine) as db:
        create_admin(db, USER, PASSWORD, min_password_length=12)
        db.commit()
    return migrated_engine


@pytest.fixture
def client(engine_with_admin: Engine) -> Generator[TestClient]:
    with TestClient(
        create_app(make_settings(), engine=engine_with_admin), follow_redirects=False
    ) as c:
        yield c


def login(client: TestClient, username: str = USER, password: str = PASSWORD) -> int:
    token = csrf_from(client.get("/admin/login").text)
    r = client.post(
        "/admin/login", data={"username": username, "password": password, "csrf_token": token}
    )
    return r.status_code


def page_csrf(client: TestClient) -> str:
    return csrf_from(client.get("/admin/").text)


# --- password hashing -------------------------------------------------------


def test_hash_is_argon2id_and_verifies() -> None:
    h = hash_password("hunter2hunter2")
    assert h.startswith("$argon2id$") and "hunter2" not in h
    assert verify_password(h, "hunter2hunter2") is True
    assert verify_password(h, "wrong") is False


def test_verify_garbage_hash_is_false_not_error() -> None:
    assert verify_password("not-a-hash", "x") is False


# --- create_admin -----------------------------------------------------------


def test_create_admin_stores_argon2_hash(migrated_engine: Engine) -> None:
    with DbSession(migrated_engine) as db:
        create_admin(db, "bob", "a-long-enough-password", min_password_length=12)
        db.commit()
        user = db.query(AdminUser).filter_by(username="bob").one()
        assert user.password_hash.startswith("$argon2id$")


def test_create_admin_duplicate_fails_loudly(engine_with_admin: Engine) -> None:
    with DbSession(engine_with_admin) as db, pytest.raises(ValueError, match="already exists"):
        create_admin(db, USER, "another-long-password", min_password_length=12)


def test_create_admin_short_password_rejected(migrated_engine: Engine) -> None:
    with DbSession(migrated_engine) as db, pytest.raises(ValueError, match="at least 12"):
        create_admin(db, "bob", "short", min_password_length=12)


def test_create_admin_blank_username_rejected(migrated_engine: Engine) -> None:
    with DbSession(migrated_engine) as db, pytest.raises(ValueError, match="username"):
        create_admin(db, "  ", "a-long-enough-password", min_password_length=12)


# --- login / logout ---------------------------------------------------------


def test_login_page_renders_form_with_csrf(client: TestClient) -> None:
    r = client.get("/admin/login")
    assert r.status_code == 200
    assert 'name="username"' in r.text and 'name="password"' in r.text
    assert csrf_from(r.text)


def test_unauthenticated_access_is_blocked(client: TestClient) -> None:
    r = client.get("/admin/")
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert client.get("/admin/api/me").status_code == 401


def test_login_success_grants_access(client: TestClient) -> None:
    assert login(client) == 303
    assert client.get("/admin/api/me").json() == {"username": USER}
    landing = client.get("/admin/")
    assert landing.status_code == 200 and USER in landing.text


@pytest.mark.parametrize(
    ("username", "password"), [(USER, "wrong password!!"), ("nobody", PASSWORD)]
)
def test_login_failure_is_401_and_generic(client: TestClient, username: str, password: str) -> None:
    token = csrf_from(client.get("/admin/login").text)
    r = client.post(
        "/admin/login", data={"username": username, "password": password, "csrf_token": token}
    )
    assert r.status_code == 401
    assert "Invalid username or password" in r.text
    assert client.get("/admin/api/me").status_code == 401


def test_login_requires_valid_csrf(client: TestClient) -> None:
    client.get("/admin/login")
    for data in (
        {"username": USER, "password": PASSWORD},
        {"username": USER, "password": PASSWORD, "csrf_token": "forged"},
    ):
        assert client.post("/admin/login", data=data).status_code == 403
    assert client.get("/admin/api/me").status_code == 401


def test_login_without_prior_page_visit_is_403(client: TestClient) -> None:
    r = client.post(
        "/admin/login", data={"username": USER, "password": PASSWORD, "csrf_token": "x"}
    )
    assert r.status_code == 403


def test_csrf_token_rotates_on_login(client: TestClient) -> None:
    before = csrf_from(client.get("/admin/login").text)
    login(client)
    assert page_csrf(client) != before


def test_logout_requires_csrf_and_keeps_session_otherwise(client: TestClient) -> None:
    login(client)
    assert client.post("/admin/logout", data={}).status_code == 403
    assert client.post("/admin/logout", data={"csrf_token": "forged"}).status_code == 403
    assert client.get("/admin/api/me").status_code == 200


def test_logout_ends_session(client: TestClient) -> None:
    login(client)
    r = client.post("/admin/logout", data={"csrf_token": page_csrf(client)})
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert client.get("/admin/api/me").status_code == 401


# --- cookie ----------------------------------------------------------------


def test_cookie_flags(engine_with_admin: Engine) -> None:
    with TestClient(
        create_app(make_settings(admin_cookie_secure=True), engine=engine_with_admin),
        base_url="https://testserver",
        follow_redirects=False,
    ) as c:
        r = c.get("/admin/login")
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    assert "max-age=43200" in cookie


def test_tampered_cookie_is_anonymous(client: TestClient) -> None:
    login(client)
    name = next(iter(client.cookies.keys()))
    client.cookies.set(name, client.cookies[name][:-3] + "abc")
    assert client.get("/admin/api/me").status_code == 401


def test_cookie_signed_with_other_key_is_rejected(
    client: TestClient, engine_with_admin: Engine
) -> None:
    login(client)
    other = create_app(make_settings(secret_key="o" * 32), engine=engine_with_admin)
    with TestClient(other, follow_redirects=False) as c2:
        c2.cookies.update(client.cookies)
        assert c2.get("/admin/api/me").status_code == 401


def test_session_expires(engine_with_admin: Engine) -> None:
    app = create_app(make_settings(admin_session_max_age_s=1), engine=engine_with_admin)
    with TestClient(app, follow_redirects=False) as c:
        assert login(c) == 303
        assert c.get("/admin/api/me").status_code == 200
        time.sleep(2.1)
        assert c.get("/admin/api/me").status_code == 401


# --- CSRF on JSON routes ----------------------------------------------------


def test_json_csrf_dependency(engine_with_admin: Engine) -> None:
    from app.auth import csrf_protected

    app: FastAPI = create_app(make_settings(), engine=engine_with_admin)

    @app.post("/admin/api/_probe", dependencies=[Depends(csrf_protected)])
    def probe() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app, follow_redirects=False) as c:
        assert c.post("/admin/api/_probe").status_code == 401  # not logged in
        login(c)
        token = page_csrf(c)
        assert c.post("/admin/api/_probe").status_code == 403
        assert c.post("/admin/api/_probe", headers={"X-CSRF-Token": "forged"}).status_code == 403
        assert c.post("/admin/api/_probe", headers={"X-CSRF-Token": token}).json() == {"ok": True}
