"""Admin analysis pages: start, list, show a run, and the results on the participant page
(PLAN §6, §9; DECISIONS 2026-10-14)."""

import re
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as Db

from app.jobs import enqueue, mark_failed
from app.models import Job, JobKind, JobStatus, Study, StudyMode, Video
from tests.adminutil import Admin, anonymous_client, csrf_from, logged_in_admin
from tests.analysis_fake import make_run
from worker.analysis.config import AnalysisConfig
from worker.analysis.run import start_run
from worker.analysis.select import Selection


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def admin(migrated_engine: Engine, data_root: Path) -> Generator[Admin]:
    with logged_in_admin(migrated_engine, data_root) as a:
        yield a


@pytest.fixture
def seeded(migrated_engine: Engine) -> None:
    with Db(migrated_engine) as db:
        db.add(Study(id=1, name="Spring", code="spring", mode=StudyMode.REMOTE, config={}))
        db.add(Video(id=1, title="Dot video", file_path="x", duration=10.0, width=16, height=9,
                     fps=25.0, sha256="0" * 64))  # fmt: skip
        db.commit()


def form(admin: Admin, **fields: str) -> dict[str, str]:
    token = csrf_from(admin.client.get("/admin/analysis").text)
    return {"study_id": "1", "video_id": "1", "csrf_token": token, **fields}


def test_the_pages_need_a_login(migrated_engine: Engine, data_root: Path) -> None:
    run_id = make_run(data_root)
    with anonymous_client(migrated_engine, data_root) as stranger:
        for path in ("/admin/analysis", f"/admin/analysis/1/1/{run_id}", "/admin/api/analysis"):
            assert stranger.get(path).status_code in (303, 401), path
        assert stranger.post(
            "/admin/analysis", data={"study_id": "1", "video_id": "1"}
        ).status_code in (
            303,
            401,
            403,
        )


def test_the_form_queues_a_run_and_the_list_shows_it(
    admin: Admin, seeded: None, migrated_engine: Engine
) -> None:
    c = admin.client
    page = c.get("/admin/analysis")
    assert page.status_code == 200 and "Spring (spring)" in page.text and "Dot video" in page.text
    assert "<script" not in page.text and "No runs yet." in page.text

    assert c.post("/admin/analysis", data={**form(admin), "csrf_token": "bad"}).status_code == 403
    r = c.post("/admin/analysis", data=form(admin, gaze_columns="drift_corrected",
                                            aoi_tolerance_k="1.5"))  # fmt: skip
    assert r.status_code == 303 and r.headers["location"] == "/admin/analysis?queued=true"
    listed = c.get(r.headers["location"]).text
    assert "Queued." in listed and "queued" in listed and "drift_corrected" in listed
    assert "Spring (spring)" in listed and "Dot video" in listed
    with Db(migrated_engine) as db:
        (job,) = db.scalars(select(Job)).all()
        assert job.kind is JobKind.ANALYZE_VIDEO and job.status is JobStatus.QUEUED
        assert job.args["gaze_columns"] == "drift_corrected" and job.args["aoi_tolerance_k"] == 1.5

    again = c.post("/admin/analysis", data=form(admin))  # one at a time per video and study
    assert again.status_code == 409 and "already queued or running" in again.text
    assert "already queued or running" in c.post("/admin/analysis", data=form(admin)).text


def test_the_json_api_starts_and_lists_runs(admin: Admin, seeded: None, data_root: Path) -> None:
    c = admin.client
    body = {"study_id": 1, "video_id": 1}
    assert c.post("/admin/api/analysis", json=body).status_code == 403  # no CSRF header
    r = c.post("/admin/api/analysis", json=body, headers=admin.headers)
    assert r.status_code == 201 and re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{6}", r.json()["run_id"])
    assert c.post("/admin/api/analysis", json=body, headers=admin.headers).status_code == 409
    assert c.post("/admin/api/analysis", json={**body, "video_id": 9},
                  headers=admin.headers).status_code == 404  # fmt: skip
    assert c.post("/admin/api/analysis", json={**body, "gaze_columns": "x"},
                  headers=admin.headers).status_code in (409, 422)  # fmt: skip
    done = make_run(data_root)
    listed = c.get("/admin/api/analysis").json()
    assert {x["run_id"] for x in listed} == {r.json()["run_id"], done}
    by_id = {x["run_id"]: x for x in listed}
    assert by_id[r.json()["run_id"]]["status"] == "queued"
    assert by_id[done]["status"] == "done" and by_id[done]["included"] == 1
    assert by_id[done]["excluded"] == 1
    assert [x["run_id"] for x in c.get("/admin/api/analysis?study_id=2").json()] == []
    record = c.get(f"/admin/api/analysis/1/1/{done}").json()
    assert record["status"] == "done" and record["run_id"] == done


def test_the_run_page_shows_everything_the_run_made(
    admin: Admin, seeded: None, data_root: Path
) -> None:
    run_id = make_run(data_root, codes=("P1", "P2"))
    page = admin.client.get(f"/admin/analysis/1/1/{run_id}")
    assert page.status_code == 200
    html = page.text
    base = f"/admin/analysis/1/1/{run_id}"
    assert f'src="{base}/files/heatmap.mp4"' in html and "<video controls" in html
    assert f'src="{base}/files/static/001_window_1.png"' in html
    assert f'src="{base}/files/synchrony.png"' in html
    assert (
        f'href="{base}/download.zip"' in html
        and f'href="{base}/files/aoi_metrics_wide.csv"' in html
    )
    assert "dwell_sample_s" in html and "P1" in html and "P2" in html  # the CSV preview
    assert "Left out" in html and "not_processed" in html
    assert "not recorded" in html  # no git hash in this test environment
    assert "3 of 5 windows" in html and "Mean dispersion 0.125" in html
    assert "<script" not in html
    for src in re.findall(r'(?:src|href)="(/admin/analysis[^"]+)"', html):
        assert admin.client.get(src).status_code == 200, src


def test_a_failed_run_shows_why(admin: Admin, seeded: None, data_root: Path) -> None:
    run_id = make_run(data_root, files={}, error="no included sessions with gaze on this video")
    html = admin.client.get(f"/admin/analysis/1/1/{run_id}").text
    assert "Failed: no included sessions with gaze on this video" in html
    assert "Nobody was included." not in html  # the fake run still lists one included session
    listed = admin.client.get("/admin/analysis").text
    assert "failed: no included sessions" in listed


def test_an_unknown_run_is_404(admin: Admin, seeded: None) -> None:
    assert admin.client.get("/admin/analysis/1/1/20260101T000000Z-aaaaaa").status_code == 404


def test_the_participant_page_shows_the_latest_run_that_included_them(
    admin: Admin, seeded: None, data_root: Path
) -> None:
    make_run(data_root, codes=("P1",), run_id="20260101T000000Z-aaaaaa")
    latest = make_run(data_root, codes=("P1", "P2"), run_id="20260102T000000Z-bbbbbb")
    failed = make_run(
        data_root, codes=("P1",), files={}, error="boom", run_id="20260103T000000Z-cccccc"
    )
    html = admin.client.get("/admin/participants/1/P1").text
    assert "<h2>Analysis</h2>" in html
    assert f"/admin/analysis/1/1/{latest}" in html  # newest finished run, not the failed one
    assert failed not in html and "20260101T000000Z-aaaaaa" not in html
    assert "Mean NSS" in html and "0.50" in html and "dwell_sample_s" in html
    results = admin.client.get("/admin/api/participants/1/P1").json()
    assert results["analysis"][0]["run_id"] == latest
    assert results["analysis"][0]["aoi_rows"] == [
        {"participant_code": "P1", "aoi_id": "a", "dwell_sample_s": "1.5"}
    ]
    assert admin.client.get("/admin/participants/1/P3").text.count("<h2>Analysis</h2>") == 0


def test_a_run_whose_worker_died_and_whose_job_failed_is_listed_as_failed(
    admin: Admin, seeded: None, data_root: Path, migrated_engine: Engine
) -> None:
    run_id = "20261020T080000Z-abcdef"
    start_run(data_root, 1, 1, run_id, AnalysisConfig(), Selection([], []))  # `running`, no end
    with Db(migrated_engine) as db:
        job = enqueue(db, JobKind.ANALYZE_VIDEO, {"study_id": 1, "video_id": 1, "run_id": run_id})
        mark_failed(db, job, "worker died during attempt 3 of 3")
        db.commit()
    (listed,) = admin.client.get("/admin/api/analysis").json()
    assert listed["status"] == "failed" and "worker died" in listed["error"]
    assert "failed: worker died" in admin.client.get("/admin/analysis").text


def test_the_dots_checkbox_reaches_the_job(
    admin: Admin, seeded: None, migrated_engine: Engine
) -> None:
    c = admin.client
    assert c.post("/admin/analysis", data=form(admin, heatmap_dots="true")).status_code == 303
    with Db(migrated_engine) as db:
        (job,) = db.scalars(select(Job)).all()
        assert job.args["heatmap_dots"] is True
        assert job.args["gaze_columns"] == "drift_corrected"  # the form's default
    api = c.post("/admin/api/analysis", json={"study_id": 1, "video_id": 1, "heatmap_dots": True},
                 headers=admin.headers)  # fmt: skip
    assert api.status_code == 409  # one at a time; the option itself is accepted by the schema
