"""The `compare_pipelines` job, its admin pages and the CLI (PLAN §9.5, §6)."""

import io
import json
import logging
import re
import shutil
import uuid
import zipfile
from collections.abc import Generator, Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as Db
from typer.testing import CliRunner

import cli
from app.analysis_runs import queue_comparison
from app.db import make_session_factory
from app.jobs import enqueue
from app.models import Job, JobKind, JobStatus, SessionStatus
from app.models import Session as SessionRow
from app.pipeline_b import summary_of
from app.service_errors import ConflictError, NotFoundError
from app.storage import compare_run_dir, derived_dir, session_dir
from tests.adminutil import Admin, anonymous_client, csrf_from, logged_in_admin
from tests.compare_data import Numbers, add_session, add_study
from tests.dbutil import make_settings
from tests.session_fixture import FixtureSession
from worker.analysis.config import AnalysisConfig
from worker.handlers import make_compare_pipelines
from worker.run import process_one

runner = CliRunner()
A8 = [0.10, 0.12, 0.09, 0.15, 0.11, 0.13, 0.14, 0.08]
B8 = [0.08, 0.10, 0.10, 0.11, 0.09, 0.10, 0.12, 0.07]


@pytest.fixture(autouse=True)
def no_leftover_log_handlers() -> Iterator[None]:
    yield
    logging.getLogger().handlers.clear()


@pytest.fixture
def study(migrated_engine: Engine, tmp_path: Path) -> Path:
    """Study 1 with eight sessions that have both pipelines and one that has only A."""
    add_study(migrated_engine)
    for i, (a, b) in enumerate(zip(A8, B8, strict=True)):
        add_session(migrated_engine, tmp_path, f"P{i}", Numbers(a, a, a * 1000, 20.0, 0.7),
                    Numbers(b, b, b * 1000, 25.0, 0.8))  # fmt: skip
    add_session(migrated_engine, tmp_path, "NB", Numbers(0.1, 0.1, 100.0, 10.0, 0.5), None)
    return tmp_path


def run_job(engine: Engine, root: Path) -> tuple[Job, str]:
    factory = make_session_factory(engine)
    with factory() as db:
        job, run_id = queue_comparison(db, 1)
        db.commit()
        job_id = job.id
    handler = make_compare_pipelines(factory, make_settings(data_root=root))
    assert process_one(factory, {JobKind.COMPARE_PIPELINES: handler}) is True
    with factory() as db:
        return db.get_one(Job, job_id), run_id


def record_of(root: Path, run_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((compare_run_dir(root, 1, run_id) / "run.json").read_text())
    return data


def test_the_job_compares_and_lists_the_session_that_has_no_b(
    migrated_engine: Engine, study: Path
) -> None:
    job, run_id = run_job(migrated_engine, study)
    assert job.status is JobStatus.DONE, job.log
    data = record_of(study, run_id)
    assert data["status"] == "done" and len(data["included"]) == 8
    assert [e["reasons"] for e in data["excluded"]] == [["pipeline_b_not_run"]]


def test_a_study_with_nothing_to_compare_fails_the_job_with_the_reason(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    add_study(migrated_engine)
    add_session(migrated_engine, tmp_path, "NB", Numbers(0.1, 0.1, 100.0, 10.0, 0.5), None)
    job, run_id = run_job(migrated_engine, tmp_path)
    assert job.status is JobStatus.FAILED and "no session has both" in job.log
    assert record_of(tmp_path, run_id)["status"] == "failed"


def test_a_requeued_comparison_starts_again_and_a_finished_one_is_never_overwritten(
    migrated_engine: Engine, study: Path
) -> None:
    factory = make_session_factory(migrated_engine)
    handler = make_compare_pipelines(factory, make_settings(data_root=study))
    with factory() as db:
        job, run_id = queue_comparison(db, 1)
        db.commit()
        job_id = job.id
    stale = compare_run_dir(study, 1, run_id)
    stale.mkdir(parents=True)
    (stale / "run.json").write_text(json.dumps({"status": "running"}))
    (stale / "left_behind.csv").write_text("x")
    assert process_one(factory, {JobKind.COMPARE_PIPELINES: handler}) is True
    with factory() as db:
        assert db.get_one(Job, job_id).status is JobStatus.DONE
    assert record_of(study, run_id)["status"] == "done" and not (stale / "left_behind.csv").exists()

    again = Job(kind=JobKind.COMPARE_PIPELINES, args={"study_id": 1, "run_id": run_id})
    with pytest.raises(FileExistsError, match="already finished"):
        handler(again)  # the run id belongs to a finished comparison


def test_the_handler_checks_its_arguments(migrated_engine: Engine, study: Path) -> None:
    handler = make_compare_pipelines(make_session_factory(migrated_engine),
                                     make_settings(data_root=study))  # fmt: skip
    for args in ({"run_id": "20261021T100000Z-abcdef"}, {"study_id": 1, "run_id": "../x"},
                 {"study_id": 1, "run_id": 5}):  # fmt: skip
        with pytest.raises(ValueError):
            handler(Job(kind=JobKind.COMPARE_PIPELINES, args=args))
    with pytest.raises(LookupError):
        handler(Job(kind=JobKind.COMPARE_PIPELINES,
                    args={"study_id": 99, "run_id": "20261021T100000Z-abcdef"}))  # fmt: skip


def test_queueing_checks_the_study_and_allows_one_comparison_at_a_time(
    migrated_engine: Engine, study: Path
) -> None:
    with Db(migrated_engine) as db:
        with pytest.raises(NotFoundError):
            queue_comparison(db, 99)
        job, run_id = queue_comparison(db, 1)
        db.commit()
        assert job.args == {"study_id": 1, "run_id": run_id}
        with pytest.raises(ConflictError, match="already queued or running"):
            queue_comparison(db, 1)
        job.status = JobStatus.DONE
        db.commit()
        queue_comparison(db, 1)


@pytest.fixture
def admin(migrated_engine: Engine, study: Path) -> Generator[Admin]:
    with logged_in_admin(migrated_engine, study) as a:
        yield a


def test_the_pages_files_and_zip_of_a_comparison(
    migrated_engine: Engine, study: Path, admin: Admin
) -> None:
    _, run_id = run_job(migrated_engine, study)
    c = admin.client
    base = f"/admin/comparisons/1/{run_id}"
    page = c.get(base)
    assert page.status_code == 200
    html = page.text
    assert "Pipeline comparison" in html and "accuracy_norm" in html and "P0" in html
    assert f'src="{base}/files/boxplots.png"' in html and "pipeline_b_not_run" in html
    assert "<script" not in html
    for src in re.findall(r'(?:src|href)="(/admin/comparisons[^"]+)"', html):
        assert c.get(src).status_code == 200, src
    assert c.get(f"{base}/files/report.md").headers["content-type"].startswith("text/markdown")
    for bad in ("secret.txt", "../run.json", "..%2Frun.json", "nope.csv"):
        assert c.get(f"{base}/files/{bad}").status_code == 404, bad
    z = c.get(f"{base}/download.zip")
    with zipfile.ZipFile(io.BytesIO(z.content)) as archive:
        assert archive.testzip() is None
        assert {n.split("/", 1)[1] for n in archive.namelist()} == {
            "run.json", "comparison.csv", "statistics.csv", "report.md", "boxplots.png",
        }  # fmt: skip
    api = c.get("/admin/api/comparisons").json()
    assert [r["run_id"] for r in api] == [run_id] and api[0]["status"] == "done"
    assert api[0]["compared"] == 8 and api[0]["left_out"] == 1
    assert c.get(f"/admin/api/comparisons/1/{run_id}").json()["kind"] == "compare_pipelines"
    assert c.get("/admin/api/comparisons?study_id=2").json() == []
    assert run_id in c.get("/admin/analysis").text
    assert c.get("/admin/api/analysis").json() == []  # a comparison is not an analysis run


def test_a_comparison_is_started_from_the_page_and_the_api(
    migrated_engine: Engine, study: Path, admin: Admin
) -> None:
    c = admin.client
    token = csrf_from(c.get("/admin/analysis").text)
    assert (
        c.post("/admin/comparisons", data={"study_id": "1", "csrf_token": "x"}).status_code == 403
    )
    r = c.post("/admin/comparisons", data={"study_id": "1", "csrf_token": token})
    assert r.status_code == 303 and r.headers["location"] == "/admin/analysis?queued=true"
    again = c.post("/admin/comparisons", data={"study_id": "1", "csrf_token": token})
    assert again.status_code == 409 and "already queued or running" in again.text
    assert c.post("/admin/api/comparisons", json={"study_id": 1}).status_code == 403
    assert (
        c.post("/admin/api/comparisons", json={"study_id": 9}, headers=admin.headers).status_code
        == 404
    )
    with Db(migrated_engine) as db:
        (job,) = db.scalars(select(Job)).all()
        assert job.kind is JobKind.COMPARE_PIPELINES and job.status is JobStatus.QUEUED
    listed = c.get("/admin/api/comparisons").json()
    assert listed[0]["status"] == "queued" and listed[0]["has_record"] is False


def test_a_comparison_whose_worker_died_is_listed_as_failed(
    migrated_engine: Engine, study: Path, admin: Admin
) -> None:
    run_id = "20261021T110000Z-abcdef"
    stale = compare_run_dir(study, 1, run_id)
    stale.mkdir(parents=True)
    record = {
        "run_id": run_id, "study_id": 1, "status": "running", "error": None,
        "started_at": "2026-10-21T11:00:00+00:00", "included": [], "excluded": [],
    }  # fmt: skip
    (stale / "run.json").write_text(json.dumps(record))
    with Db(migrated_engine) as db:
        job = enqueue(db, JobKind.COMPARE_PIPELINES, {"study_id": 1, "run_id": run_id})
        job.status, job.log = JobStatus.FAILED, "worker died during attempt 3 of 3"
        db.commit()
    (row,) = admin.client.get("/admin/api/comparisons").json()
    assert row["status"] == "failed"


def test_the_endpoints_need_a_login(migrated_engine: Engine, study: Path) -> None:
    run_id = "20261021T110000Z-abcdef"
    base = f"/admin/comparisons/1/{run_id}"
    paths = [f"/admin/api/comparisons/1/{run_id}", f"{base}/download.zip", f"{base}/files/run.json",
             "/admin/api/comparisons"]  # fmt: skip
    with anonymous_client(migrated_engine, study) as stranger:
        for path in paths:
            assert stranger.get(path).status_code in (303, 401), path
        posted = stranger.post("/admin/api/comparisons", json={"study_id": 1})
        assert posted.status_code in (401, 403)


def test_the_cli_compares_here_and_now(
    migrated_engine: Engine, study: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(data_root=study)
    monkeypatch.setattr(cli, "settings", lambda: settings)
    monkeypatch.setattr(cli, "session_factory", lambda: make_session_factory(migrated_engine))
    monkeypatch.setattr(cli, "open_session", lambda: make_session_factory(migrated_engine)())
    result = runner.invoke(cli.app, ["compare", "1"])
    assert result.exit_code == 0, result.output
    assert "8 session(s), 1 left out" in result.output and "accuracy_norm" in result.output
    assert "left out NB: pipeline_b_not_run" in result.output
    (run_dir,) = (study / "analysis" / "1" / "compare").iterdir()
    assert (run_dir / "report.md").is_file()
    with Db(migrated_engine) as db:  # an open comparison blocks another one
        queue_comparison(db, 1)
        db.commit()
    again = runner.invoke(cli.app, ["compare", "1"])
    assert again.exit_code == 1 and "already queued or running" in again.output


def test_the_real_session_with_both_pipelines_copied_as_three_sessions(
    migrated_engine: Engine, b_session: FixtureSession, tmp_path: Path
) -> None:
    """Real MediaPipe, real small gaze network: the numbers in the files are the numbers compared;
    three pairs are fewer than the six a p-value needs."""
    root = tmp_path / "data"
    study_id = b_session.study_id
    add_study(migrated_engine, study_id)
    source = derived_dir(b_session.root, study_id, b_session.session_id)
    a_report = json.loads((source / "quality.json").read_text())
    for n in range(3):
        sid = uuid.UUID(int=0xC000 + n)
        shutil.copytree(
            session_dir(b_session.root, study_id, b_session.session_id),
            session_dir(root, study_id, sid),
        )
        with Db(migrated_engine) as db:
            db.add(
                SessionRow(
                    id=sid, study_id=study_id, participant_code=f"R{n}", upload_token_hash="h",
                    status=SessionStatus.PROCESSED, flags=[],
                    quality={**a_report, "pipeline_b": summary_of(source)},
                )
            )  # fmt: skip
            db.commit()
    job, run_id = run_job_for(migrated_engine, root, study_id)
    assert job.status is JobStatus.DONE, job.log
    data = json.loads((compare_run_dir(root, study_id, run_id) / "run.json").read_text())
    accuracy = data["results"]["statistics"][0]
    b_report = json.loads((source / "quality_b.json").read_text())
    assert accuracy["n"] == 3 and accuracy["p_value"] is None
    assert accuracy["median_a"] == pytest.approx(a_report["validation"]["mean_error_norm"])
    assert accuracy["median_b"] == pytest.approx(b_report["validation"]["mean_error_norm"])
    assert AnalysisConfig().compare_min_pairs == 6


def run_job_for(engine: Engine, root: Path, study_id: int) -> tuple[Job, str]:
    factory = make_session_factory(engine)
    with factory() as db:
        job, run_id = queue_comparison(db, study_id)
        db.commit()
        job_id = job.id
    handler = make_compare_pipelines(factory, make_settings(data_root=root))
    assert process_one(factory, {JobKind.COMPARE_PIPELINES: handler}) is True
    with factory() as db:
        return db.get_one(Job, job_id), run_id
