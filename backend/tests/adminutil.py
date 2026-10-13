"""Helpers for tests of the admin routes: an app with an admin account and a logged-in client."""

import re
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session as DbSession

from app.auth import create_admin
from app.main import create_app
from tests.dbutil import make_settings

USER, PASSWORD = "alice", "correct horse battery"


def csrf_from(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, html
    return m.group(1)


@dataclass
class Admin:
    client: TestClient
    csrf: str
    data_root: Path

    @property
    def headers(self) -> dict[str, str]:
        return {"X-CSRF-Token": self.csrf}


@contextmanager
def anonymous_client(engine: Engine, data_root: Path, **settings: object) -> Generator[TestClient]:
    with DbSession(engine) as db:
        create_admin(db, USER, PASSWORD, min_password_length=12)
        db.commit()
    app = create_app(make_settings(data_root=data_root, **settings), engine=engine)
    with TestClient(app, follow_redirects=False) as client:
        yield client


@contextmanager
def logged_in_admin(engine: Engine, data_root: Path, **settings: object) -> Generator[Admin]:
    with anonymous_client(engine, data_root, **settings) as client:
        token = csrf_from(client.get("/admin/login").text)
        r = client.post(
            "/admin/login", data={"username": USER, "password": PASSWORD, "csrf_token": token}
        )
        assert r.status_code == 303
        yield Admin(client, csrf_from(client.get("/admin/").text), data_root)
