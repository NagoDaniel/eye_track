"""Admin: running Pipeline B for a session or a study, and what the session page shows of it
(PLAN §6, §12.2; DECISIONS 2026-10-20)."""

import json
import shutil
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as Db

from app.models import Job, JobKind, JobStatus, SessionStatus, Study
from app.models import Session as SessionRow
from app.pipeline_b import B_STAGE_NAMES, STALE_FILE, summary_of
from app.storage import derived_dir
from tests.adminutil import Admin, anonymous_client, csrf_from, logged_in_admin
from tests.session_db import seed_session
from tests.session_fixture import FixtureSession

MODEL = "l2cs_mobileone_s0"


def copy_of(s: FixtureSession, tmp_path: Path) -> FixtureSession:
    root = tmp_path / "data"
    shutil.copytree(s.root, root)
    return FixtureSession(
        root, s.study_id, s.session_id, s.manifest, root / s.raw.relative_to(s.root)
    )


def seed_with_report(engine: Engine, s: FixtureSession, status: SessionStatus) -> None:
    seed_session(engine, s, status=status)
    derived = derived_dir(s.root, s.study_id, s.session_id)
    report = json.loads((derived / "quality.json").read_text())
    b = summary_of(derived)
    with Db(engine) as db:
        row = db.get_one(SessionRow, s.session_id)
        row.quality = report if b is None else {**report, "pipeline_b": b}
        db.commit()


@pytest.fixture
def a_only(migrated_engine: Engine, a_session: FixtureSession, tmp_path: Path) -> FixtureSession:
    s = copy_of(a_session, tmp_path)
    seed_with_report(migrated_engine, s, SessionStatus.PROCESSED)
    return s


@pytest.fixture
def admin(migrated_engine: Engine, a_only: FixtureSession, model_b_root: Path) -> Generator[Admin]:
    with logged_in_admin(
        migrated_engine, a_only.root, models_root=model_b_root, pipeline_b_model=MODEL
    ) as a:
        yield a


def page(admin: Admin, s: FixtureSession) -> str:
    return admin.client.get(f"/admin/sessions/{s.session_id}").text


def b_section(html: str) -> str:
    return html.split("<h2>Pipeline B</h2>")[1].split("<h2>Debug plots</h2>")[0]


def test_the_page_offers_pipeline_b_when_it_can_run(admin: Admin, a_only: FixtureSession) -> None:
    html = b_section(page(admin, a_only))
    assert "Pipeline B has not been run for this session" in html
    assert html.count("not run") == len(B_STAGE_NAMES)
    assert "Cannot be run now" not in html and " disabled" not in html
    assert "Run Pipeline B</button>" in html and html.count("Run from here") == 3


def test_the_button_says_why_it_is_disabled(
    migrated_engine: Engine, a_only: FixtureSession, tmp_path: Path
) -> None:
    (tmp_path / "empty").mkdir()
    with logged_in_admin(
        migrated_engine, a_only.root, models_root=tmp_path / "empty", pipeline_b_model=MODEL
    ) as admin:
        html = b_section(page(admin, a_only))
        assert "Cannot be run now" in html and "scripts/fetch_models.sh" in html
        assert html.count(" disabled") == len(B_STAGE_NAMES)
        r = admin.client.post(
            f"/admin/api/sessions/{a_only.session_id}/run-b", json={}, headers=admin.headers
        )
        assert r.status_code == 409 and "fetch_models.sh" in r.json()["detail"]
    with Db(migrated_engine) as db:
        assert db.scalars(select(Job)).all() == []


def test_a_session_a_has_not_processed_cannot_be_run(
    admin: Admin, a_only: FixtureSession, migrated_engine: Engine
) -> None:
    with Db(migrated_engine) as db:
        db.get_one(SessionRow, a_only.session_id).status = SessionStatus.UPLOADED
        db.commit()
    assert "Pipeline A must have processed it" in b_section(page(admin, a_only))
    r = admin.client.post(
        f"/admin/api/sessions/{a_only.session_id}/run-b", json={}, headers=admin.headers
    )
    assert r.status_code == 409


def test_the_api_queues_a_job_once_and_checks_its_input(
    admin: Admin, a_only: FixtureSession, migrated_engine: Engine
) -> None:
    c, sid = admin.client, a_only.session_id
    url = f"/admin/api/sessions/{sid}/run-b"
    assert c.post(url, json={}).status_code == 403  # no CSRF header
    assert c.post(f"/admin/api/sessions/{uuid.uuid4()}/run-b", json={},
                  headers=admin.headers).status_code == 404  # fmt: skip
    assert c.post(url, json={"from_stage": "plots"}, headers=admin.headers).status_code == 422
    assert c.post(url, json={"x": 1}, headers=admin.headers).status_code == 422

    r = c.post(url, json={"from_stage": "calibration_b"}, headers=admin.headers)
    assert r.status_code == 201 and r.json()["args"] == {
        "session_id": str(sid), "from_stage": "calibration_b",
    }  # fmt: skip
    again = c.post(url, json={}, headers=admin.headers)
    assert again.status_code == 409 and "already queued" in again.json()["detail"]
    with Db(migrated_engine) as db:
        (job,) = db.scalars(select(Job)).all()
        assert job.kind is JobKind.PROCESS_PIPELINE_B and job.status is JobStatus.QUEUED
    assert "Pipeline B</td>" in page(admin, a_only)  # the job is listed
    # while it is queued, A cannot be re-run either: one job per session at a time
    rerun = c.post(f"/admin/api/sessions/{sid}/rerun", json={}, headers=admin.headers)
    assert rerun.status_code == 409


def test_the_form_queues_and_comes_back_to_the_page(
    admin: Admin, a_only: FixtureSession, migrated_engine: Engine
) -> None:
    c = admin.client
    token = csrf_from(page(admin, a_only))
    url = f"/admin/sessions/{a_only.session_id}/run-b"
    assert c.post(url, data={"csrf_token": "bad"}).status_code == 403
    r = c.post(url, data={"csrf_token": token, "from_stage": "features_b"})
    assert r.status_code == 303 and r.headers["location"].endswith("?queued=true")
    with Db(migrated_engine) as db:
        (job,) = db.scalars(select(Job)).all()
        assert job.args["from_stage"] == "features_b"


def test_the_endpoints_need_a_login(migrated_engine: Engine, a_only: FixtureSession) -> None:
    with anonymous_client(migrated_engine, a_only.root) as stranger:
        for path in (
            f"/admin/api/sessions/{a_only.session_id}/run-b",
            f"/admin/sessions/{a_only.session_id}/run-b",
            "/admin/api/studies/1/run-b",
            "/admin/studies/1/run-b",
        ):
            assert stranger.post(path, json={}).status_code in (303, 401, 403), path


def test_a_session_with_pipeline_b_shows_its_numbers_and_its_stale_mark(
    migrated_engine: Engine, b_session: FixtureSession, model_b_root: Path, tmp_path: Path
) -> None:
    s = copy_of(b_session, tmp_path)
    seed_with_report(migrated_engine, s, SessionStatus.PROCESSED)
    with logged_in_admin(
        migrated_engine, s.root, models_root=model_b_root, pipeline_b_model=MODEL
    ) as admin:
        html = b_section(page(admin, s))
        assert "Pipeline B has not been run" not in html
        assert f"<th>Network</th><td>{MODEL}</td>" in html
        assert '<span class="ok">yes</span>' in html  # included for B (loose thresholds)
        assert html.count('<span class="ok">ok</span>') == len(B_STAGE_NAMES)
        assert "stale" not in html
        detail = admin.client.get(f"/admin/api/sessions/{s.session_id}").json()
        assert detail["pipeline_b"]["summary"]["model"] == MODEL
        assert detail["pipeline_b"]["stale"] is None

        (derived_dir(s.root, s.study_id, s.session_id) / STALE_FILE).write_text(
            json.dumps({"since": "2026-10-20T00:00:00+00:00", "by_stage": "sync"})
        )
        stale = b_section(page(admin, s))
        assert "is <strong>stale</strong>" in stale and 'Pipeline A ran "sync"' in stale


def test_the_study_button_queues_every_processed_session_that_can_take_it(
    migrated_engine: Engine,
    a_session: FixtureSession,
    model_b_root: Path,
    tmp_path: Path,
) -> None:
    s = copy_of(a_session, tmp_path)
    seed_with_report(migrated_engine, s, SessionStatus.PROCESSED)
    other = uuid.uuid4()
    with Db(migrated_engine) as db:  # a second session of the study, still uploading
        db.add(
            SessionRow(
                id=other, study_id=s.study_id, participant_code="ZZ0101", upload_token_hash="h",
                status=SessionStatus.UPLOADED, flags=[],
            )
        )  # fmt: skip
        db.get_one(Study, s.study_id).config = {"playlist": []}  # the study page needs one
        db.commit()
    with logged_in_admin(
        migrated_engine, s.root, models_root=model_b_root, pipeline_b_model=MODEL
    ) as admin:
        c = admin.client
        url = f"/admin/api/studies/{s.study_id}/run-b"
        assert c.post(url).status_code == 403
        assert c.post("/admin/api/studies/999/run-b", headers=admin.headers).status_code == 404
        r = c.post(url, headers=admin.headers)
        assert r.status_code == 201
        assert r.json() == {"queued": [str(s.session_id)], "left_out": []}  # not the uploading one
        again = c.post(url, headers=admin.headers).json()
        assert again["queued"] == [] and "already queued" in again["left_out"][0]["reason"]

        token = csrf_from(c.get(f"/admin/studies/{s.study_id}").text)
        assert (
            c.post(f"/admin/studies/{s.study_id}/run-b", data={"csrf_token": "x"}).status_code
            == 403
        )
        form = c.post(f"/admin/studies/{s.study_id}/run-b", data={"csrf_token": token})
        assert form.status_code == 303 and "b_queued=0&b_left=1" in form.headers["location"]
        shown = c.get(form.headers["location"]).text
        assert "Queued for 0 session(s); 1 could not be queued" in shown
        assert "Run Pipeline B for all processed sessions" in shown
    with Db(migrated_engine) as db:
        assert len(db.scalars(select(Job)).all()) == 1
