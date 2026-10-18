"""The admin session inspector (PLAN §12.2): quality report, plots, stage table, re-run."""

import json
import re
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as DbSession

from app.models import Job, JobKind, JobStatus, SessionStatus, Study, StudyMode
from app.models import Session as SessionRow
from app.pipeline_names import PLOT_FILES, STAGE_NAMES
from app.sessions_view import inclusion_of, quality_summary, stage_rows
from tests.adminutil import Admin, logged_in_admin
from tests.synthetic_session import Synthetic, build
from worker.pipeline.config import PipelineConfig
from worker.pipeline.gaze import run_gaze
from worker.pipeline.plots import run_plots
from worker.pipeline.quality import build_quality

CONFIG = PipelineConfig()


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def admin(migrated_engine: Engine, data_root: Path) -> Generator[Admin]:
    with logged_in_admin(migrated_engine, data_root) as a:
        yield a


@pytest.fixture
def processed(migrated_engine: Engine, data_root: Path) -> Synthetic:
    """A processed session on disk (derived files, report, plots) and in the database."""
    s = build(data_root)
    run_gaze(s.root, s.study_id, s.session_id, CONFIG)
    report = build_quality(s.root, s.study_id, s.session_id, CONFIG)
    summary = run_plots(s.root, s.study_id, s.session_id, CONFIG)
    report["stages"] = {
        "plots": {"seconds": 1.2, "summary": summary.as_dict()},
        "gaze": {"seconds": 0.4, "summary": {"frames": 880}},
        "calibration": {"seconds": 0.1, "summary": {"skipped": "calibration_failed"}},
    }
    with DbSession(migrated_engine) as db:
        db.add(Study(id=s.study_id, name="S", code="abc", mode=StudyMode.REMOTE, config={}))
        db.flush()
        db.add(SessionRow(id=s.session_id, study_id=s.study_id, participant_code="AB0315",
                          upload_token_hash="h", status=SessionStatus.PROCESSED, quality=report,
                          flags=["sync_short"]))  # fmt: skip
        db.commit()
    return s


def anonymous(admin: Admin) -> TestClient:
    """Another browser on the same app: no login cookie."""
    return TestClient(admin.client.app)


def page(admin: Admin, s: Synthetic, query: str = "") -> str:
    r = admin.client.get(f"/admin/sessions/{s.session_id}{query}")
    assert r.status_code == 200, r.text
    return r.text


def set_status(engine: Engine, s: Synthetic, status: SessionStatus) -> None:
    with DbSession(engine) as db:
        db.get_one(SessionRow, s.session_id).status = status
        db.commit()


def jobs(engine: Engine) -> list[Job]:
    with DbSession(engine) as db:
        return list(db.scalars(select(Job).order_by(Job.id)))


# --- the page ---


def test_the_page_shows_the_report_the_stages_and_the_plots(
    admin: Admin, processed: Synthetic
) -> None:
    html = page(admin, processed)
    assert "Quality report" in html and "Included in the analysis" in html
    assert "Valid gaze during playback" in html and "Validation error" in html
    assert "before_video" in html and "after_last_video" in html  # the validation blocks
    for stage in STAGE_NAMES:
        assert f"<td>{stage}</td>" in html, stage
    # One re-run button per stage, each a form with the CSRF token and the stage name.
    forms = re.findall(r'action="/admin/sessions/[^"]+/rerun">(.*?)</form>', html, re.S)
    assert len(forms) == len(STAGE_NAMES)
    assert all('name="csrf_token"' in f and "disabled" not in f for f in forms)
    stages = re.findall(r'name="from_stage" value="(\w+)"', "".join(forms))
    assert stages == list(STAGE_NAMES)
    assert "skipped: calibration_failed" in html  # a skipped stage says so
    images = re.findall(r'<img class="plot" src="([^"]+)"', html)
    assert [i.rsplit("/", 1)[1] for i in images] == list(PLOT_FILES)


def test_the_quality_numbers_come_from_the_report(admin: Admin, processed: Synthetic) -> None:
    html = page(admin, processed)
    report = json.loads((data_derived(processed) / "quality.json").read_text())
    valid = round(report["playback"]["valid_fraction"] * 100)
    assert f"{valid} %" in html
    assert f"{report['webcam']['fps_mean']:.1f} frames/s" in html
    assert "1 / 1" in html  # one fullscreen exit, one stall in the designed session


def data_derived(s: Synthetic) -> Path:
    return s.root / "studies" / "1" / "sessions" / str(s.session_id) / "derived"


def test_a_session_that_was_not_processed_says_so(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    with DbSession(migrated_engine) as db:
        db.add(Study(id=1, name="S", code="abc", mode=StudyMode.REMOTE, config={}))
        db.flush()
        sid = uuid.UUID(int=3)
        db.add(SessionRow(id=sid, study_id=1, participant_code="AB0315", upload_token_hash="h",
                          status=SessionStatus.UPLOADED))  # fmt: skip
        db.commit()
    html = admin.client.get(f"/admin/sessions/{sid}").text
    assert "No quality report yet" in html and "No plots yet" in html
    assert html.count("not run") == len(STAGE_NAMES)
    assert html.count("Re-run from here") == len(
        STAGE_NAMES
    )  # an uploaded session can be processed


def test_a_failed_job_shows_why(
    admin: Admin, migrated_engine: Engine, processed: Synthetic
) -> None:
    with DbSession(migrated_engine) as db:
        db.add(
            Job(
                kind=JobKind.PROCESS_SESSION,
                args={"session_id": str(processed.session_id), "from_stage": "sync"},
                status=JobStatus.FAILED,
                attempts=1,
                log="stage 'sync' failed: SyncError: no recorder_start",
            )
        )
        db.commit()
    html = page(admin, processed)
    assert "Why it failed" in html and "no recorder_start" in html
    assert "<td>sync</td>" in html


def test_the_lists_show_whether_a_session_is_included(admin: Admin, processed: Synthetic) -> None:
    rows = admin.client.get("/admin/api/sessions").json()
    assert rows[0]["included"] is True and rows[0]["inclusion_reasons"] == []
    assert ">yes<" in admin.client.get("/admin/sessions").text
    participant = admin.client.get("/admin/participants/1/AB0315").text
    assert "included" in participant and "quality report and plots" in participant


# --- the plot files ---


def test_a_plot_is_served_to_admins_only(
    admin: Admin, processed: Synthetic, migrated_engine: Engine, data_root: Path
) -> None:
    url = f"/admin/sessions/{processed.session_id}/debug/gaze.png"
    r = admin.client.get(url)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "no-store" and r.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert anonymous(admin).get(url).status_code == 401


def test_only_the_known_plot_names_and_existing_files_are_served(
    admin: Admin, processed: Synthetic
) -> None:
    base = f"/admin/sessions/{processed.session_id}/debug"
    for name in (
        "quality.json",
        "..%2Fquality.json",
        "%2e%2e%2f%2e%2e%2fraw%2Fmeta.json",
        "gaze.PNG",
        "other.png",
    ):
        assert admin.client.get(f"{base}/{name}").status_code == 404, name
    (data_derived(processed) / "debug" / "gaze.png").unlink()
    assert admin.client.get(f"{base}/gaze.png").status_code == 404
    images = re.findall(r'<img class="plot" src="[^"]*/([^"/]+)"', page(admin, processed))
    assert "gaze.png" not in images and len(images) == len(PLOT_FILES) - 1  # only what exists
    unknown = admin.client.get(f"/admin/sessions/{uuid.UUID(int=99)}/debug/gaze.png")
    assert unknown.status_code == 404


# --- re-running ---


def post_rerun(
    admin: Admin, s: Synthetic, from_stage: str = "gaze", csrf: str | None = None
) -> Any:
    return admin.client.post(
        f"/admin/sessions/{s.session_id}/rerun",
        data={"csrf_token": admin.csrf if csrf is None else csrf, "from_stage": from_stage},
        follow_redirects=False,
    )


def test_the_button_queues_the_job_and_the_page_waits_for_it(
    admin: Admin, processed: Synthetic, migrated_engine: Engine
) -> None:
    r = post_rerun(admin, processed, "gaze")
    assert (
        r.status_code == 303
        and r.headers["location"] == f"/admin/sessions/{processed.session_id}?queued=true"
    )
    (job,) = jobs(migrated_engine)
    assert job.kind is JobKind.PROCESS_SESSION and job.status is JobStatus.QUEUED
    assert job.args == {"session_id": str(processed.session_id), "from_stage": "gaze"}
    html = page(admin, processed, "?queued=true")
    assert "Processing is queued" in html and "already queued or running" in html
    # No second job while one is open: the stage buttons and the replay button are off.
    assert html.count(" disabled") == len(STAGE_NAMES) + 1
    assert post_rerun(admin, processed).status_code == 409


def test_from_the_start_and_after_the_job_finished(
    admin: Admin, processed: Synthetic, migrated_engine: Engine
) -> None:
    assert post_rerun(admin, processed, "").status_code == 303
    assert jobs(migrated_engine)[0].args == {"session_id": str(processed.session_id)}
    with DbSession(migrated_engine) as db:
        db.get_one(Job, jobs(migrated_engine)[0].id).status = JobStatus.DONE
        db.commit()
    assert post_rerun(admin, processed, "sync").status_code == 303  # allowed again
    assert len(jobs(migrated_engine)) == 2


def test_the_form_checks_csrf_the_stage_and_the_login(
    admin: Admin, processed: Synthetic, migrated_engine: Engine, data_root: Path
) -> None:
    assert post_rerun(admin, processed, csrf="wrong").status_code == 403
    assert post_rerun(admin, processed, "bogus").status_code == 422
    assert jobs(migrated_engine) == []
    r = anonymous(admin).post(
        f"/admin/sessions/{processed.session_id}/rerun",
        data={"csrf_token": "x", "from_stage": "gaze"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
    assert jobs(migrated_engine) == []


@pytest.mark.parametrize(
    "status",
    [
        SessionStatus.CREATED,
        SessionStatus.RECORDING,
        SessionStatus.PROCESSING,
        SessionStatus.WITHDRAWN,
    ],
)
def test_a_session_that_is_not_finished_cannot_be_run_again(
    admin: Admin, processed: Synthetic, migrated_engine: Engine, status: SessionStatus
) -> None:
    set_status(migrated_engine, processed, status)
    r = post_rerun(admin, processed)
    assert r.status_code == 409 and status.value in r.json()["detail"]
    assert jobs(migrated_engine) == []
    assert " disabled" in page(admin, processed)


def test_the_json_api(
    admin: Admin, processed: Synthetic, migrated_engine: Engine, data_root: Path
) -> None:
    url = f"/admin/api/sessions/{processed.session_id}/rerun"
    assert admin.client.post(url, json={"from_stage": "sync"}).status_code == 403  # no CSRF header
    r = admin.client.post(url, json={"from_stage": "sync"}, headers=admin.headers)
    assert r.status_code == 201 and r.json()["args"] == {
        "session_id": str(processed.session_id),
        "from_stage": "sync",
    }
    assert admin.client.post(url, json={}, headers=admin.headers).status_code == 409  # one is open
    assert (
        admin.client.post(
            f"/admin/api/sessions/{uuid.UUID(int=9)}/rerun", json={}, headers=admin.headers
        ).status_code
        == 404
    )
    assert (
        admin.client.post(
            url, json={"from_stage": "sync", "extra": 1}, headers=admin.headers
        ).status_code
        == 422
    )
    assert anonymous(admin).post(url, json={}).status_code in (401, 403)


# --- pure helpers ---


def test_stage_rows_list_every_stage_in_order() -> None:
    recorded = {
        "gaze": {"seconds": 2.0, "summary": {"frames": 5}},
        "calibration": {"seconds": 0.1, "summary": {"skipped": "x"}},
    }
    rows = stage_rows({"stages": recorded})
    assert [r["name"] for r in rows] == list(STAGE_NAMES)
    by_name = {r["name"]: r for r in rows}
    assert (
        by_name["gaze"]["ran"]
        and by_name["gaze"]["seconds"] == 2.0
        and by_name["gaze"]["skipped"] is None
    )
    assert by_name["calibration"]["skipped"] == "x"
    assert not by_name["ingest"]["ran"] and by_name["ingest"]["seconds"] is None
    assert all(not r["ran"] for r in stage_rows(None))


def test_summaries_without_a_report_or_inclusion() -> None:
    assert quality_summary(None) is None and inclusion_of(None) is None
    assert inclusion_of({"inclusion": "garbage"}) is None
    assert quality_summary({})["inclusion"]["included"] is None  # type: ignore[index]


# --- the debug replay and the webcam recording ---


def put_video(path: Path, codec: str = "libx264") -> Path:
    from tests.videos import make_video

    return make_video(path, seconds=1.0, size="160x120", codec=codec)


def replay_file(s: Synthetic) -> Path:
    return data_derived(s) / "debug" / "replay.mp4"


def webcam_file(s: Synthetic) -> Path:
    return data_derived(s).parent / "raw" / "webcam.webm"


def test_the_page_offers_the_replay_and_says_it_shows_the_participant(
    admin: Admin, processed: Synthetic
) -> None:
    html = page(admin, processed)
    assert "Both show the participant's webcam video" in html
    assert "Render the debug replay" in html and "Not rendered yet" in html
    assert "<video" not in html  # nothing plays until it exists
    put_video(replay_file(processed))
    html = page(admin, processed)
    assert 'src="/admin/sessions/' in html and "replay.mp4" in html and "Render it again" in html


def test_the_replay_button_queues_one_render_job(
    admin: Admin, processed: Synthetic, migrated_engine: Engine
) -> None:
    r = admin.client.post(f"/admin/sessions/{processed.session_id}/render-debug",
                          data={"csrf_token": admin.csrf}, follow_redirects=False)  # fmt: skip
    assert r.status_code == 303 and r.headers["location"].endswith("?queued=true")
    (job,) = jobs(migrated_engine)
    assert job.kind is JobKind.RENDER_DEBUG and job.args == {
        "session_id": str(processed.session_id)
    }
    # While it is open neither another render nor a re-run of the stages is allowed.
    again = admin.client.post(f"/admin/sessions/{processed.session_id}/render-debug",
                              data={"csrf_token": admin.csrf}, follow_redirects=False)  # fmt: skip
    assert again.status_code == 409 and post_rerun(admin, processed).status_code == 409
    html = page(admin, processed)
    assert "debug replay" in html and "already queued or running" in html


def test_the_replay_needs_a_processed_session_with_gaze(
    admin: Admin, processed: Synthetic, migrated_engine: Engine
) -> None:
    url = f"/admin/api/sessions/{processed.session_id}/render-debug"
    assert admin.client.post(url).status_code == 403  # no CSRF header
    set_status(migrated_engine, processed, SessionStatus.UPLOADED)
    refused = admin.client.post(url, headers=admin.headers)
    assert refused.status_code == 409 and "only a processed session" in refused.json()["detail"]
    set_status(migrated_engine, processed, SessionStatus.PROCESSED)
    (data_derived(processed) / "gaze_a.parquet").unlink()
    refused = admin.client.post(url, headers=admin.headers)
    assert refused.status_code == 409 and "no gaze" in refused.json()["detail"]
    assert (
        admin.client.post(
            f"/admin/api/sessions/{uuid.UUID(int=9)}/render-debug", headers=admin.headers
        ).status_code
        == 404
    )
    assert jobs(migrated_engine) == []


def test_the_replay_is_served_with_range_requests_to_admins_only_and_logged(
    admin: Admin, processed: Synthetic, caplog: pytest.LogCaptureFixture
) -> None:
    video = put_video(replay_file(processed))
    url = f"/admin/sessions/{processed.session_id}/debug/replay.mp4"
    with caplog.at_level("INFO", logger="admin"):
        whole = admin.client.get(url)
        part = admin.client.get(url, headers={"Range": "bytes=0-99"})
    assert whole.status_code == 200 and whole.headers["content-type"] == "video/mp4"
    assert (
        whole.headers["accept-ranges"] == "bytes" and whole.headers["cache-control"] == "no-store"
    )
    assert whole.content == video.read_bytes()
    assert (
        part.status_code == 206
        and part.headers["content-range"] == f"bytes 0-99/{video.stat().st_size}"
    )
    assert part.content == video.read_bytes()[:100]
    served = [r for r in caplog.records if r.name == "admin"]
    assert [r.getMessage() for r in served] == ["participant video served to an admin"] * 2
    assert all(getattr(r, "session_id", None) == str(processed.session_id) for r in served)
    assert anonymous(admin).get(url).status_code == 401


def test_the_webcam_recording_is_shown_on_request_with_a_warning(
    admin: Admin, processed: Synthetic
) -> None:
    assert "Show the webcam recording" not in page(admin, processed)  # not assembled yet
    assert "has not been assembled" in page(admin, processed)
    put_video(webcam_file(processed), codec="libvpx")
    assert "Show the webcam recording" in page(admin, processed)
    shown = admin.client.get(f"/admin/sessions/{processed.session_id}/webcam")
    assert shown.status_code == 200
    assert (
        "This is the participant's webcam video" in shown.text and "<video controls" in shown.text
    )
    assert f"/admin/sessions/{processed.session_id}/webcam.webm" in shown.text


def test_the_webcam_file_supports_seeking_and_is_for_admins_only(
    admin: Admin, processed: Synthetic
) -> None:
    video = put_video(webcam_file(processed), codec="libvpx")
    url = f"/admin/sessions/{processed.session_id}/webcam.webm"
    part = admin.client.get(url, headers={"Range": "bytes=10-59"})
    assert part.status_code == 206 and part.headers["content-type"] == "video/webm"
    assert part.content == video.read_bytes()[10:60]
    assert anonymous(admin).get(url).status_code == 401
    assert (
        anonymous(admin)
        .get(f"/admin/sessions/{processed.session_id}/webcam", follow_redirects=False)
        .status_code
        == 303
    )


def test_a_deleted_or_missing_webcam_recording_is_not_served(
    admin: Admin, processed: Synthetic, migrated_engine: Engine
) -> None:
    url = f"/admin/sessions/{processed.session_id}/webcam.webm"
    assert admin.client.get(url).status_code == 404  # never assembled
    put_video(webcam_file(processed), codec="libvpx")
    with DbSession(migrated_engine) as db:
        from datetime import UTC, datetime

        db.get_one(SessionRow, processed.session_id).raw_video_deleted_at = datetime.now(UTC)
        db.commit()
    assert admin.client.get(url).status_code == 404
    assert admin.client.get(f"/admin/sessions/{processed.session_id}/webcam").status_code == 404
    assert "was deleted on" in page(admin, processed) and "Show the webcam recording" not in page(
        admin, processed
    )
