import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as DbSession
from typer.testing import CliRunner

import cli
from app.db import make_session_factory
from app.models import AdminUser

runner = CliRunner()


@pytest.fixture(autouse=True)
def use_test_db(migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "open_session", lambda: make_session_factory(migrated_engine)())
    monkeypatch.setattr(cli, "min_password_length", lambda: 12)
    monkeypatch.setattr(cli, "log_level", lambda: "INFO")


def test_create_admin_with_password_from_stdin(migrated_engine: Engine) -> None:
    r = runner.invoke(
        cli.app, ["create-admin", "--username", "carol", "--password-stdin"],
        input="a-long-enough-password\n",
    )  # fmt: skip
    assert r.exit_code == 0, r.output
    assert "carol" in r.output
    with DbSession(migrated_engine) as db:
        user = db.query(AdminUser).filter_by(username="carol").one()
        assert user.password_hash.startswith("$argon2id$")


def test_create_admin_interactive_prompt(migrated_engine: Engine) -> None:
    r = runner.invoke(
        cli.app, ["create-admin", "--username", "dave"],
        input="a-long-enough-password\na-long-enough-password\n",
    )  # fmt: skip
    assert r.exit_code == 0, r.output


def test_duplicate_admin_exits_nonzero(migrated_engine: Engine) -> None:
    args = ["create-admin", "--username", "erin", "--password-stdin"]
    assert runner.invoke(cli.app, args, input="a-long-enough-password\n").exit_code == 0
    r = runner.invoke(cli.app, args, input="a-long-enough-password\n")
    assert r.exit_code == 1 and "already exists" in r.output


def test_short_password_exits_nonzero() -> None:
    r = runner.invoke(
        cli.app, ["create-admin", "--username", "frank", "--password-stdin"], input="short\n"
    )
    assert r.exit_code == 1 and "at least 12" in r.output
