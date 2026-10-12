"""Backend CLI: `docker compose exec api python cli.py <command>` (PLAN §11.4)."""

import sys

import typer
from sqlalchemy.orm import Session

from app.auth import create_admin
from app.db import make_engine, make_session_factory
from app.logging import configure_logging
from app.settings import Settings

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def open_session() -> Session:
    return make_session_factory(make_engine(_settings().database_url))()


def log_level() -> str:
    return _settings().log_level


def min_password_length() -> int:
    return _settings().admin_min_password_length


@app.callback()
def main() -> None:
    """eyetrack administration commands."""
    configure_logging(log_level())


@app.command("create-admin")
def create_admin_command(
    username: str = typer.Option(..., help="Login name for the new admin."),
    password_stdin: bool = typer.Option(
        False, "--password-stdin", help="Read the password from stdin instead of prompting."
    ),
) -> None:
    """Create an admin account (argon2-hashed password)."""
    if password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    with open_session() as db:
        try:
            create_admin(db, username, password, min_password_length())
        except ValueError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        db.commit()
    typer.echo(f"Created admin '{username.strip()}'.")


if __name__ == "__main__":
    app()
