"""The study page shows what the worker does for it and the analyses made with it, and a job can
be cancelled from there (DECISIONS 2026-10-22)."""

import json
import re
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db

from app.jobs import enqueue
from app.models import Job, JobKind, JobStatus, Video
from app.storage import compare_run_dir
from tests.adminutil import Admin, anonymous_client, csrf_from, logged_in_admin
from tests.analysis_fake import make_run
from tests.compare_data import Numbers, add_session, add_study

N = Numbers(0.1, 0.1, 100.0, 10.0, 0.5)


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def admin(migrated_engine: Engine, data_root: Path) -> Generator[Admin]:
    with logged_in_admin(migrated_engine, data_root) as a:
        yield a


def add_job(
    engine: Engine,
    kind: JobKind,
    status: JobStatus,
    args: dict[str, object],
    log: str = "",
    cancel: bool = False,
) -> int:
    with Db(engine) as db:
        job = enqueue(db, kind, args)
        job.status, job.log, job.cancel_requested = status, log, cancel
        db.commit()
        return job.id


@pytest.fixture
def study(migrated_engine: Engine, data_root: Path) -> dict[str, int | str]:
    """Study 1 with two participants, jobs in every state and one other study with no jobs."""
    add_study(migrated_engine, 1)
    add_study(migrated_engine, 2)
    with Db(migrated_engine) as db:
        db.add(Video(id=1, title="Moving dot", file_path="x", duration=5.0, width=16, height=9,
                     fps=25.0, sha256="0" * 64))  # fmt: skip
        db.commit()
    a = add_session(migrated_engine, data_root, "AA0101", N, N)
    b = add_session(migrated_engine, data_root, "BB0101", N, N)
    ids = {
        "running": add_job(migrated_engine, JobKind.PROCESS_PIPELINE_B, JobStatus.RUNNING,
                           {"session_id": str(a)}),
        "queued": add_job(migrated_engine, JobKind.PROCESS_PIPELINE_B, JobStatus.QUEUED,
                          {"session_id": str(b), "from_stage": "calibration_b"}),
        "analysis": add_job(migrated_engine, JobKind.ANALYZE_VIDEO, JobStatus.QUEUED,
                            {"study_id": 1, "video_id": 1, "run_id": "r"}),
        "done": add_job(migrated_engine, JobKind.PROCESS_SESSION, JobStatus.DONE,
                        {"session_id": str(a)}),
        "failed": add_job(migrated_engine, JobKind.COMPARE_PIPELINES, JobStatus.FAILED,
                          {"study_id": 1, "run_id": "q"}, log="Traceback: boom"),
        "cancelled": add_job(migrated_engine, JobKind.RENDER_DEBUG, JobStatus.FAILED,
                             {"session_id": str(b)}, log="cancelled by the admin"),
        "other": add_job(migrated_engine, JobKind.ANALYZE_VIDEO, JobStatus.RUNNING,
                         {"study_id": 2, "video_id": 1, "run_id": "s"}),
    }  # fmt: skip
    return {**ids, "a": str(a), "b": str(b)}


def test_the_studies_list_says_which_study_is_busy(
    admin: Admin, study: dict[str, int | str]
) -> None:
    html = admin.client.get("/admin/studies").text
    rows = re.findall(r"<tr>(.*?)</tr>", html, flags=re.S)
    first = next(r for r in rows if '/admin/studies/1"' in r)
    second = next(r for r in rows if '/admin/studies/2"' in r)
    assert "1 running" in first and "2 queued" in first and "#jobs" in first
    assert "1 running" in second and "queued" not in second.split("running")[1]
    assert "<script" not in html


def test_the_study_page_lists_the_jobs_with_cancel_buttons_for_the_open_ones(
    admin: Admin, study: dict[str, int | str]
) -> None:
    html = admin.client.get("/admin/studies/1").text
    jobs = html.split('<h2 id="jobs">')[1].split("<h2>Analyses of this study</h2>")[0]
    for label in ("Pipeline B", "Analysis", "Processing (Pipeline A)", "Pipeline comparison"):
        assert label in jobs
    assert "AA0101" in jobs and "BB0101" in jobs and "from calibration_b" in jobs
    assert "video: Moving dot" in jobs and "the whole study" in jobs
    open_ids = [study["running"], study["queued"], study["analysis"]]
    for job_id in open_ids:
        assert f'action="/admin/jobs/{job_id}/cancel"' in jobs
    for job_id in (study["done"], study["failed"], study["cancelled"], study["other"]):
        assert f'action="/admin/jobs/{job_id}/cancel"' not in jobs  # finished, or another study's
    assert jobs.count("Cancel</button>") == 3
    assert "<strong>running</strong>" in jobs and "cancelled</span>" in jobs
    assert "Why it failed" in jobs and "Traceback: boom" in jobs  # a real failure keeps its log
    running_row = jobs.index(f'<td class="num">{study["running"]}</td>')
    queued_row = jobs.index(f'<td class="num">{study["queued"]}</td>')
    assert running_row < queued_row  # what runs is listed before what waits
    assert f'<td class="num">{study["other"]}</td>' not in jobs


def test_cancelling_a_queued_job_ends_it_and_a_running_one_shows_stopping(
    admin: Admin, study: dict[str, int | str], migrated_engine: Engine
) -> None:
    c = admin.client
    queued, running = study["queued"], study["running"]
    r = c.post(f"/admin/api/jobs/{queued}/cancel", headers=admin.headers)
    assert r.status_code == 200 and r.json() == {"job_id": queued, "result": "cancelled"}
    r = c.post(f"/admin/api/jobs/{running}/cancel", headers=admin.headers)
    assert r.json()["result"] == "cancelling"
    with Db(migrated_engine) as db:
        assert db.get_one(Job, queued).status is JobStatus.FAILED
        assert db.get_one(Job, running).cancel_requested is True
        assert db.get_one(Job, running).status is JobStatus.RUNNING  # the worker ends it
    jobs = c.get("/admin/studies/1").text.split('<h2 id="jobs">')[1]
    assert "stopping..." in jobs
    assert f'action="/admin/jobs/{running}/cancel"' not in jobs  # already asked
    again = c.post(f"/admin/api/jobs/{running}/cancel", headers=admin.headers)
    assert again.status_code == 200 and again.json()["result"] == "cancelling"  # harmless


def test_a_finished_or_unknown_job_cannot_be_cancelled_and_the_request_is_protected(
    admin: Admin, study: dict[str, int | str], migrated_engine: Engine
) -> None:
    c = admin.client
    done = c.post(f"/admin/api/jobs/{study['done']}/cancel", headers=admin.headers)
    assert done.status_code == 409 and "nothing to cancel" in done.json()["detail"]
    assert c.post("/admin/api/jobs/99999/cancel", headers=admin.headers).status_code == 404
    assert c.post(f"/admin/api/jobs/{study['queued']}/cancel").status_code == 403  # no CSRF header
    with Db(migrated_engine) as db:
        assert db.get_one(Job, study["queued"]).status is JobStatus.QUEUED


def test_the_form_cancels_and_comes_back_to_a_page_of_the_admin_only(
    admin: Admin, study: dict[str, int | str], migrated_engine: Engine
) -> None:
    c = admin.client
    token = csrf_from(c.get("/admin/studies/1").text)
    url = f"/admin/jobs/{study['queued']}/cancel"
    assert c.post(url, data={"csrf_token": "x"}).status_code == 403
    r = c.post(url, data={"csrf_token": token, "back": "/admin/studies/1#jobs"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/studies/1#jobs"
    for hostile in ("https://evil.example/", "//evil.example/x", "/elsewhere"):
        with Db(migrated_engine) as db:
            job = enqueue(db, JobKind.PROCESS_PIPELINE_B, {"session_id": study["a"]})
            db.commit()
            job_id = job.id
        r = c.post(f"/admin/jobs/{job_id}/cancel", data={"csrf_token": token, "back": hostile})
        assert r.status_code == 303 and r.headers["location"] == "/admin/studies", hostile


def test_the_endpoints_need_a_login(migrated_engine: Engine, data_root: Path) -> None:
    with anonymous_client(migrated_engine, data_root) as stranger:
        assert stranger.post("/admin/api/jobs/1/cancel").status_code in (401, 403)
        assert stranger.post("/admin/jobs/1/cancel", data={"csrf_token": "x"}).status_code in (
            303,
            401,
            403,
        )


def test_the_study_page_refers_to_its_analyses_and_comparisons(
    admin: Admin, study: dict[str, int | str], data_root: Path
) -> None:
    run = make_run(data_root, study_id=1, video_id=1, run_id="20261022T100000Z-aaaaaa")
    make_run(data_root, study_id=2, video_id=1, run_id="20261022T100000Z-bbbbbb")  # another study
    compare_id = "20261022T110000Z-cccccc"
    directory = compare_run_dir(data_root, 1, compare_id)
    directory.mkdir(parents=True)
    (directory / "run.json").write_text(json.dumps({
        "run_id": compare_id, "study_id": 1, "status": "done", "error": None,
        "started_at": "2026-10-22T11:00:00+00:00", "included": [{}, {}], "excluded": [{}],
        "kind": "compare_pipelines", "outputs": [], "parameters": {}, "results": {},
    }))  # fmt: skip
    html = admin.client.get("/admin/studies/1").text
    section = html.split("<h2>Analyses of this study</h2>")[1].split("<h2>Pipeline B</h2>")[0]
    assert f'href="/admin/analysis/1/1/{run}"' in section and "Moving dot" in section
    assert "20261022T100000Z-bbbbbb" not in section  # the other study's run is not shown
    assert f'href="/admin/comparisons/1/{compare_id}"' in section
    assert 'href="/admin/analysis"' in section
    empty = admin.client.get("/admin/studies/2").text
    assert "20261022T110000Z-cccccc" not in empty
