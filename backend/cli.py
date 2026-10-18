"""Backend CLI: `docker compose exec api python cli.py <command>` (PLAN §11.4, §12.3)."""

import json
import sys
import uuid
from pathlib import Path

import pyarrow.parquet as pq
import typer
from sqlalchemy.orm import Session, sessionmaker

from app.auth import create_admin
from app.db import make_engine, make_session_factory
from app.events import parse_events
from app.logging import configure_logging
from app.models import Job, JobKind
from app.models import Session as SessionRow
from app.session_archive import ArchiveError, export_session, import_session
from app.settings import Settings
from app.storage import derived_dir, events_path
from worker.handlers import make_process_session
from worker.pipeline.config import PipelineConfig
from worker.pipeline.gaze import GAZE_FILE
from worker.pipeline.moving_dot import DotPath, MovingDotError, format_report, moving_dot_report
from worker.pipeline.plots import plot_moving_dot, png_bytes
from worker.pipeline.runner import STAGE_NAMES

app = typer.Typer(no_args_is_help=True, add_completion=False)


def settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def session_factory() -> sessionmaker[Session]:
    return make_session_factory(make_engine(settings().database_url))


def open_session() -> Session:
    return session_factory()()


def log_level() -> str:
    return settings().log_level


def min_password_length() -> int:
    return settings().admin_min_password_length


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


def _session_id(text: str) -> uuid.UUID:
    try:
        return uuid.UUID(text)
    except ValueError as exc:
        typer.echo(f"error: '{text}' is not a session id (a UUID)", err=True)
        raise typer.Exit(1) from exc


@app.command("process")
def process_command(
    session_id: str = typer.Argument(..., help="The session to process."),
    from_stage: str | None = typer.Option(
        None, "--from-stage", help=f"Start at this stage: {', '.join(STAGE_NAMES)}."
    ),
    only: str | None = typer.Option(None, "--only", help="Run this stage only."),
    keep_intermediates: bool = typer.Option(
        False, "--keep-intermediates", help="Keep files a stage normally removes (for debugging)."
    ),
) -> None:
    """Run the processing pipeline for a session here and now (PLAN §8, §12.3)."""
    sid = _session_id(session_id)
    if from_stage is not None and only is not None:
        typer.echo("error: give --from-stage or --only, not both", err=True)
        raise typer.Exit(1)
    for given in (from_stage, only):
        if given is not None and given not in STAGE_NAMES:
            typer.echo(
                f"error: unknown stage '{given}' (stages: {', '.join(STAGE_NAMES)})", err=True
            )
            raise typer.Exit(1)
    config = settings()
    factory = session_factory()
    args: dict[str, object] = {"session_id": str(sid), "keep_intermediates": keep_intermediates}
    if from_stage is not None:
        args["from_stage"] = from_stage
    if only is not None:
        args["only"] = only
    handler = make_process_session(factory, config)
    try:
        handler(Job(kind=JobKind.PROCESS_SESSION, args=args))
    except Exception as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    with factory() as db:
        row = db.get(SessionRow, sid)
        report = None if row is None else row.quality
    typer.echo(f"Session {sid}: {row.status.value if row else '?'}")
    for name, entry in ((report or {}).get("stages") or {}).items():
        typer.echo(f"  {name:<12} {entry['seconds']:>7.2f} s  {json.dumps(entry['summary'])[:100]}")
    if report is not None:
        inclusion = report["inclusion"]
        typer.echo(
            f"  included in the analysis: {inclusion['included']} {inclusion['reasons'] or ''}"
        )


@app.command("export-session")
def export_session_command(
    session_id: str = typer.Argument(..., help="The session to export."),
    out: Path | None = typer.Option(None, "--out", help="The ZIP file (default <session id>.zip)."),
    no_raw_video: bool = typer.Option(
        False, "--no-raw-video", help="Leave the participant's webcam video out."
    ),
) -> None:
    """Write a ZIP with the session's raw and derived files and metadata (PLAN §12.3)."""
    sid = _session_id(session_id)
    target = out or Path(f"{sid}.zip")
    with open_session() as db:
        try:
            summary = export_session(db, settings().data_root, sid, target, not no_raw_video)
        except ArchiveError as exc:
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
    typer.echo(f"Wrote {target}: {summary.files} files, {summary.bytes / 1e6:.1f} MB")
    if summary.contains_raw_video:
        typer.echo("Warning: the archive contains the participant's webcam video (personal data).")


@app.command("import-session")
def import_session_command(
    archive: Path = typer.Argument(..., help="A ZIP made by export-session."),
    force: bool = typer.Option(False, "--force", help="Replace the session if it exists here."),
) -> None:
    """Load an exported session into this instance (PLAN §12.3)."""
    with open_session() as db:
        try:
            sid = import_session(db, settings().data_root, archive, force)
        except ArchiveError as exc:
            db.rollback()
            typer.echo(f"error: {exc}", err=True)
            raise typer.Exit(1) from exc
        db.commit()
    typer.echo(f"Imported session {sid}. Process it again with: python cli.py process {sid}")


def _default_dot_path() -> Path:
    for candidate in (
        Path("/shared/moving_dot_path.json"),  # in the image
        Path(__file__).resolve().parent.parent / "e2e" / "fixtures" / "moving_dot_path.json",
    ):
        if candidate.is_file():
            return candidate
    raise typer.BadParameter("moving_dot_path.json not found; give --path")


@app.command("moving-dot-report")
def moving_dot_report_command(
    session_id: str = typer.Argument(..., help="A processed session that watched the moving dot."),
    path: Path | None = typer.Option(None, "--path", help="moving_dot_path.json (ground truth)."),
    video_id: str | None = typer.Option(None, "--video-id", help="If it watched several videos."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of text."),
    plot: Path | None = typer.Option(None, "--plot", help="Also draw the path and the gaze (PNG)."),
) -> None:
    """How far the gaze was from the moving dot: the Milestone 3 pilot check (PLAN §12.4)."""
    sid = _session_id(session_id)
    config = settings()
    with open_session() as db:
        row = db.get(SessionRow, sid)
        if row is None:
            typer.echo(f"error: session {sid} does not exist", err=True)
            raise typer.Exit(1)
        study_id = row.study_id
    gaze_file = derived_dir(config.data_root, study_id, sid) / GAZE_FILE
    if not gaze_file.is_file():
        typer.echo(f"error: {gaze_file.name} does not exist: process the session first", err=True)
        raise typer.Exit(1)
    table = pq.read_table(gaze_file)
    dot_path = DotPath.load(path or _default_dot_path())
    try:
        report = moving_dot_report(
            table,
            parse_events(events_path(config.data_root, study_id, sid).read_bytes()),
            dot_path,
            PipelineConfig(),
            video_id,
        )
    except MovingDotError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc
    if plot is not None:
        plot.write_bytes(png_bytes(plot_moving_dot(table, dot_path, report)))
        typer.echo(f"Plot written to {plot}", err=True)
    typer.echo(json.dumps(report, indent=1) if as_json else format_report(report))
    if not report["passes"]:
        raise typer.Exit(2)


if __name__ == "__main__":
    app()
