"""The admin diagnostics page (PLAN §12.3): disk, jobs, worker heartbeat, version, browsers."""

import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db

from app.diagnostics import browser_of, camera_of
from app.jobs import enqueue
from app.models import JobKind, JobStatus, Study, StudyMode, WorkerHeartbeat
from app.models import Session as SessionRow
from app.storage import events_path
from tests.adminutil import Admin, anonymous_client, logged_in_admin

CHROME_WIN = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)
EDGE = CHROME_WIN + " Edg/130.0.2849.56"
FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0"
SAFARI = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.5 Safari/605.1.15"
)
IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
OPERA = CHROME_WIN + " OPR/115.0.0.0"


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def admin(migrated_engine: Engine, data_root: Path) -> Generator[Admin]:
    with logged_in_admin(migrated_engine, data_root) as a:
        yield a


def test_browsers_are_named_with_their_major_version() -> None:
    assert browser_of(CHROME_WIN) == "Chrome 130"
    assert browser_of(EDGE) == "Edge 130"  # Edge says Chrome too
    assert browser_of(OPERA) == "Opera 115"
    assert browser_of(FIREFOX) == "Firefox 131"
    assert browser_of(SAFARI) == "Safari 17"
    assert browser_of(IPHONE) == "Safari 17"
    assert browser_of("curl/8.0") == "other" and browser_of(None) == "other"


def write_events(root: Path, study_id: int, session_id: uuid.UUID, lines: list[object]) -> None:
    path = events_path(root, study_id, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")


def recorder_start(settings: object) -> dict[str, object]:
    return {
        "t": 5,
        "type": "recorder_start",
        "data": {"mime_type": "video/webm", "settings": settings},
    }


def test_the_camera_comes_from_the_recorder_start_event(tmp_path: Path) -> None:
    sid = uuid.uuid4()
    settings = {"width": 1280, "height": 720, "frameRate": 29.97, "deviceId": "x"}
    write_events(tmp_path, 1, sid, [{"t": 1, "type": "session_start", "data": {}},
                                    recorder_start(settings)])  # fmt: skip
    assert camera_of(tmp_path, 1, sid) == {"width": 1280, "height": 720, "frameRate": 29.97}
    other = uuid.uuid4()
    write_events(tmp_path, 1, other, [{"t": 1, "type": "session_start", "data": {}}])
    assert camera_of(tmp_path, 1, other) is None  # never started recording
    assert camera_of(tmp_path, 1, uuid.uuid4()) is None  # no events file
    broken = uuid.uuid4()
    events_path(tmp_path, 1, broken).parent.mkdir(parents=True, exist_ok=True)
    events_path(tmp_path, 1, broken).write_text('{"type": "recorder_start", broken\n')
    assert camera_of(tmp_path, 1, broken) is None


def seed(engine: Engine, root: Path) -> None:
    now = datetime.now(UTC)
    with Db(engine) as db:
        db.add(Study(id=1, name="S", code="abc", mode=StudyMode.REMOTE, config={}))
        db.flush()
        for i, (ua, cores, camera) in enumerate(
            [(CHROME_WIN, 8, {"width": 1280, "height": 720, "frameRate": 30.0}),
             (EDGE, 4, {"width": 640, "height": 480, "frameRate": 15.0}),
             (SAFARI, None, None)]
        ):  # fmt: skip
            sid = uuid.UUID(int=i + 1)
            db.add(SessionRow(id=sid, study_id=1, participant_code=f"P{i}", upload_token_hash="h",
                              created_at=now - timedelta(minutes=i),
                              client_info={"user_agent": ua, "platform": "Win32",
                                           "hardware_concurrency": cores,
                                           "screen": {"width": 1920, "height": 1080}}))  # fmt: skip
            if camera is not None:
                write_events(root, 1, sid, [recorder_start(camera)])
        for status, log in [
            (JobStatus.DONE, ""), (JobStatus.QUEUED, ""), (JobStatus.RUNNING, ""),
            (JobStatus.FAILED, "x" * 400 + "Traceback: the end of the log"),
        ]:  # fmt: skip
            job = enqueue(db, JobKind.PROCESS_SESSION, {"session_id": str(uuid.UUID(int=1))})
            job.status, job.log = status, log
            if status is JobStatus.FAILED:
                job.finished_at = now
        db.add(WorkerHeartbeat(worker_id="w1", beat_at=now - timedelta(seconds=20)))
        db.commit()
    (root / "studies" / "1").mkdir(parents=True, exist_ok=True)
    (root / "studies" / "1" / "a.bin").write_bytes(b"0" * 3_000_000)
    (root / "videos").mkdir()
    (root / "videos" / "v.mp4").write_bytes(b"0" * 2048)


def test_the_page_shows_disk_jobs_worker_version_and_browsers(
    admin: Admin, migrated_engine: Engine, data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed(migrated_engine, data_root)
    monkeypatch.setenv("GIT_SHA", "abc1234-dirty")
    html = admin.client.get("/admin/diagnostics").text
    assert "<code>abc1234-dirty</code>" in html
    assert "Alive: the last heartbeat was 20 s ago" in html  # about; rounded to the second
    assert 'Running</th><td class="num">1' in html and 'Queued</th><td class="num">1' in html
    assert 'Failed</th><td class="num">1' in html
    assert "Traceback: the end of the log" in html and "x" * 400 not in html  # only the end
    assert '<code>studies</code></td><td class="num">3 MB' in html
    assert '<code>videos</code></td><td class="num">2 KB' in html
    assert '<code>analysis</code></td><td class="num">0 KB' in html  # not there yet
    assert "Chrome 130: 1, Edge 130: 1, Safari 17: 1" in html
    assert "1280 x 720 at 30 fps" in html and "640 x 480 at 15 fps" in html
    assert "1920 x 1080" in html


def test_a_dead_or_missing_worker_is_an_error(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    assert "No worker has reported yet" in admin.client.get("/admin/diagnostics").text
    with Db(migrated_engine) as db:
        db.add(WorkerHeartbeat(worker_id="w", beat_at=datetime.now(UTC) - timedelta(minutes=10)))
        db.commit()
    html = admin.client.get("/admin/diagnostics").text
    assert "Not alive: the last heartbeat was 10 min ago" in html
    assert "No sessions yet" in html and "not known" in html


def test_the_json_api_has_the_same_numbers(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    seed(migrated_engine, data_root)
    body = admin.client.get("/admin/api/diagnostics").json()
    assert body["queue"]["queued"] == 1 and body["queue"]["failed"] == 1
    assert body["worker"]["alive"] is True
    assert 3_000_000 <= body["disk"]["folders"]["studies"] < 3_001_000  # and the events files
    assert body["disk"]["free"] > 0
    assert body["browsers"]["counts"] == {"Chrome 130": 1, "Edge 130": 1, "Safari 17": 1}


def test_the_page_and_the_api_need_a_login(migrated_engine: Engine, data_root: Path) -> None:
    with anonymous_client(migrated_engine, data_root) as stranger:
        page = stranger.get("/admin/diagnostics")
        assert page.status_code == 303 and page.headers["location"] == "/admin/login"
        assert stranger.get("/admin/api/diagnostics").status_code in (401, 403)
