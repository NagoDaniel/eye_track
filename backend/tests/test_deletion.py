"""Deleting a session, or a study with its data (DECISIONS 2026-11-02): rows, files and refusals."""

import shutil
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session as DbSession

from app.deletion import delete_session, delete_study_with_data
from app.models import (
    Chunk,
    ChunkStream,
    Job,
    JobKind,
    JobStatus,
    SessionStatus,
    Study,
    StudyMode,
)
from app.models import Session as SessionRow
from app.service_errors import ConflictError, InvalidError, NotFoundError
from tests.adminutil import Admin, logged_in_admin


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def admin(migrated_engine: Engine, data_root: Path) -> Generator[Admin]:
    with logged_in_admin(migrated_engine, data_root) as a:
        yield a


def sid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def add_study(engine: Engine, study_id: int, code: str) -> None:
    with DbSession(engine) as db:
        db.add(
            Study(
                id=study_id,
                name=f"Study {code}",
                code=code,
                mode=StudyMode.REMOTE,
                config={"playlist": []},
            )
        )
        db.commit()


def add_session(
    engine: Engine,
    root: Path,
    study_id: int,
    n: int,
    job: JobStatus | None = JobStatus.DONE,
) -> Path:
    """A session with chunk rows, a job of its own and files on disk; returns its directory."""
    directory = root / "studies" / str(study_id) / "sessions" / str(sid(n))
    for sub in ("raw", "derived"):
        (directory / sub).mkdir(parents=True)
        (directory / sub / "file.bin").write_bytes(b"data")
    with DbSession(engine) as db:
        db.add(SessionRow(id=sid(n), study_id=study_id, participant_code="AB0315",
                          upload_token_hash="h", status=SessionStatus.PROCESSED))  # fmt: skip
        db.flush()
        db.add_all(
            Chunk(session_id=sid(n), stream=ChunkStream.EVENTS, seq=k, size=4, sha256="0" * 64)
            for k in range(3)
        )
        if job is not None:
            db.add(Job(kind=JobKind.PROCESS_SESSION, args={"session_id": str(sid(n))},
                       status=job))  # fmt: skip
        db.commit()
    return directory


def add_study_outputs(engine: Engine, root: Path, study_id: int, status: JobStatus) -> Path:
    out = root / "analysis" / str(study_id) / "1" / "run-1"
    out.mkdir(parents=True)
    (out / "report.json").write_bytes(b"{}")
    with DbSession(engine) as db:
        db.add(Job(kind=JobKind.ANALYZE_VIDEO, args={"study_id": study_id, "video_id": 1},
                   status=status))  # fmt: skip
        db.commit()
    return out


def count(engine: Engine, model: type) -> int:
    with DbSession(engine) as db:
        return int(db.scalar(select(func.count()).select_from(model)) or 0)


def session_ids(engine: Engine) -> set[uuid.UUID]:
    with DbSession(engine) as db:
        return set(db.scalars(select(SessionRow.id)))


# --- one session ---


def test_deleting_a_session_removes_its_rows_and_files_and_nothing_else(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    add_study(migrated_engine, 1, "abc")
    gone = add_session(migrated_engine, data_root, 1, 1)
    kept = add_session(migrated_engine, data_root, 1, 2)
    video = data_root / "videos" / "1" / "video.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"v")

    r = admin.client.delete(f"/admin/api/sessions/{sid(1)}", headers=admin.headers)
    assert r.status_code == 204, r.text
    assert not gone.exists() and (kept / "raw" / "file.bin").is_file() and video.is_file()
    assert session_ids(migrated_engine) == {sid(2)}
    assert count(migrated_engine, Chunk) == 3 and count(migrated_engine, Job) == 1
    with DbSession(migrated_engine) as db:
        assert db.scalars(select(Job)).one().args == {"session_id": str(sid(2))}
        assert db.get(Study, 1) is not None


def test_a_session_with_a_queued_or_running_job_is_not_deleted(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    add_study(migrated_engine, 1, "abc")
    directory = add_session(migrated_engine, data_root, 1, 1, job=JobStatus.RUNNING)
    r = admin.client.delete(f"/admin/api/sessions/{sid(1)}", headers=admin.headers)
    assert r.status_code == 409 and "queued or running" in r.text
    assert directory.exists() and session_ids(migrated_engine) == {sid(1)}
    with DbSession(migrated_engine) as db:
        db.scalars(select(Job)).one().status = JobStatus.FAILED
        db.commit()
    assert (
        admin.client.delete(f"/admin/api/sessions/{sid(1)}", headers=admin.headers).status_code
        == 204
    )
    assert not directory.exists()


def test_an_unknown_session_and_an_anonymous_visitor_are_refused(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    add_study(migrated_engine, 1, "abc")
    directory = add_session(migrated_engine, data_root, 1, 1)
    r = admin.client.delete(f"/admin/api/sessions/{sid(9)}", headers=admin.headers)
    assert r.status_code == 404
    other = TestClient(admin.client.app)
    assert other.delete(f"/admin/api/sessions/{sid(1)}").status_code in (401, 403)
    assert admin.client.delete(f"/admin/api/sessions/{sid(1)}").status_code in (401, 403)  # no CSRF
    assert directory.exists() and session_ids(migrated_engine) == {sid(1)}


def test_the_session_page_asks_for_a_confirmation_before_it_deletes(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    add_study(migrated_engine, 1, "abc")
    directory = add_session(migrated_engine, data_root, 1, 1)
    html = admin.client.get(f"/admin/sessions/{sid(1)}").text
    assert f'action="/admin/sessions/{sid(1)}/delete"' in html and "cannot be undone" in html

    form = {"csrf_token": admin.csrf}
    r = admin.client.post(f"/admin/sessions/{sid(1)}/delete", data=form)
    assert r.status_code == 422 and "tick the box" in r.text and directory.exists()
    assert admin.client.post(f"/admin/sessions/{sid(1)}/delete",
                             data={"confirm": "yes"}).status_code in (400, 403)  # fmt: skip

    r = admin.client.post(f"/admin/sessions/{sid(1)}/delete", data={**form, "confirm": "yes"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/sessions?deleted=true"
    assert not directory.exists() and session_ids(migrated_engine) == set()
    assert "The session was deleted" in admin.client.get("/admin/sessions?deleted=true").text


def test_files_that_cannot_be_removed_roll_the_rows_back(
    migrated_engine: Engine, data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_study(migrated_engine, 1, "abc")
    add_session(migrated_engine, data_root, 1, 1)

    def broken(path: object) -> None:
        raise OSError("disk says no")

    monkeypatch.setattr(shutil, "rmtree", broken)
    with DbSession(migrated_engine) as db, pytest.raises(OSError, match="disk says no"):
        delete_session(db, data_root, sid(1))
        db.commit()
    assert session_ids(migrated_engine) == {sid(1)} and count(migrated_engine, Chunk) == 3
    monkeypatch.undo()
    with DbSession(migrated_engine) as db:
        delete_session(db, data_root, sid(1))  # deleting again finishes the job
        db.commit()
    assert session_ids(migrated_engine) == set()


# --- a study with everything in it ---


def test_a_study_is_deleted_with_its_sessions_jobs_and_outputs(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    add_study(migrated_engine, 1, "doomed")
    add_study(migrated_engine, 2, "safe")
    for n in (1, 2):
        add_session(migrated_engine, data_root, 1, n)
    kept = add_session(migrated_engine, data_root, 2, 3)
    outputs = add_study_outputs(migrated_engine, data_root, 1, JobStatus.DONE)
    safe_outputs = add_study_outputs(migrated_engine, data_root, 2, JobStatus.DONE)

    assert admin.client.delete("/admin/api/studies/1", headers=admin.headers).status_code == 409
    wrong = admin.client.delete("/admin/api/studies/1?confirm_code=safe", headers=admin.headers)
    assert wrong.status_code == 422 and (data_root / "studies" / "1").exists()

    r = admin.client.delete("/admin/api/studies/1?confirm_code=doomed", headers=admin.headers)
    assert r.status_code == 204, r.text
    assert not (data_root / "studies" / "1").exists() and not outputs.parent.parent.exists()
    assert (kept / "raw" / "file.bin").is_file() and (safe_outputs / "report.json").is_file()
    assert session_ids(migrated_engine) == {sid(3)} and count(migrated_engine, Chunk) == 3
    with DbSession(migrated_engine) as db:
        assert [s.code for s in db.scalars(select(Study))] == ["safe"]
        assert len(db.scalars(select(Job)).all()) == 2  # session 3's job and study 2's analysis


def test_the_study_page_asks_for_the_code_and_the_form_deletes(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    add_study(migrated_engine, 1, "doomed")
    add_session(migrated_engine, data_root, 1, 1)
    html = admin.client.get("/admin/studies/1").text
    assert "Type the study code" in html and "all its sessions" in html

    form = {"csrf_token": admin.csrf, "confirm_code": "nope"}
    r = admin.client.post("/admin/studies/1/delete", data=form)
    assert r.status_code == 422 and "does not match" in r.text and session_ids(migrated_engine)
    r = admin.client.post("/admin/studies/1/delete", data={**form, "confirm_code": " doomed "})
    assert r.status_code == 303 and r.headers["location"] == "/admin/studies"
    assert not session_ids(migrated_engine) and count(migrated_engine, Study) == 0


def test_a_study_with_an_open_job_is_not_deleted(migrated_engine: Engine, data_root: Path) -> None:
    add_study(migrated_engine, 1, "doomed")
    add_session(migrated_engine, data_root, 1, 1)
    add_study_outputs(migrated_engine, data_root, 1, JobStatus.QUEUED)
    with DbSession(migrated_engine) as db:
        with pytest.raises(ConflictError, match="queued or running"):
            delete_study_with_data(db, data_root, 1, "doomed")
        with pytest.raises(InvalidError):
            delete_study_with_data(db, data_root, 1, "")
        with pytest.raises(NotFoundError):
            delete_study_with_data(db, data_root, 9, "x")
    assert (data_root / "studies" / "1").exists() and count(migrated_engine, Study) == 1


def test_a_study_without_sessions_is_deleted_too(migrated_engine: Engine, data_root: Path) -> None:
    add_study(migrated_engine, 1, "empty")
    with DbSession(migrated_engine) as db:
        assert delete_study_with_data(db, data_root, 1, "empty") == 0
        db.commit()
    assert count(migrated_engine, Study) == 0
