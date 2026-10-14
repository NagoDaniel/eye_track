"""Participant results page and `summarize_events` (PLAN §6; DECISIONS 2026-10-14)."""

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from app.models import SessionStatus
from app.participants_view import summarize_events
from app.schemas import EventRecord
from tests.adminutil import Admin, anonymous_client, logged_in_admin
from tests.test_admin_sessions import add_chunk, add_session, add_study, gz


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def admin(migrated_engine: Engine, data_root: Path) -> Generator[Admin]:
    with logged_in_admin(migrated_engine, data_root) as a:
        yield a


def ev(t: float, type_: str, **data: Any) -> dict[str, Any]:
    return {"t": t, "type": type_, "data": data}


def records(*events: dict[str, Any]) -> list[EventRecord]:
    return [EventRecord.model_validate(e) for e in events]


def dots(phase: str, n: int, clicked: int, t0: float) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n):
        out.append(ev(t0 + i * 10, "calib_dot_show", phase=phase, id=i, x=1, y=1))
        if i < clicked:
            out.append(ev(t0 + i * 10 + 5, "calib_dot_click", phase=phase, id=i))
    return out


# --- summarize_events: pure function on hand-built event lists ---------------------------------


def test_summary_of_a_complete_session() -> None:
    events = records(
        ev(1000, "session_start", client_info={}),
        *dots("calibration", 13, 13, 2000),
        *dots("validation", 5, 5, 3000),
        ev(4000, "video_load", video_id="7", duration=3.0, width=1, height=1, rect={}),
        ev(4100, "video_play", video_id="7", media_time=0.0),
        ev(4500, "video_pause", video_id="7", media_time=0.4),
        ev(4600, "video_play", video_id="7", media_time=0.4),
        ev(4800, "video_stall", video_id="7", media_time=0.6),
        ev(5000, "fullscreen_change", fullscreen=False),
        ev(5100, "fullscreen_change", fullscreen=True),
        ev(7100, "video_ended", video_id="7"),
        ev(9000, "window_blur"),
    )
    s = summarize_events(events)
    assert s["dots"] == {
        "calibration": {"shown": 13, "clicked": 13},
        "validation": {"shown": 5, "clicked": 5},
    }
    assert s["videos"] == [
        {
            "video_id": "7",
            "loaded": True,
            "played": True,
            "ended": True,
            "watch_s": 3.0,  # first play at 4100 ms, ended at 7100 ms
            "pauses": 1,
            "stalls": 1,
        }
    ]
    assert s["fullscreen_exits"] == 1
    assert s["client_errors"] == 0
    assert s["duration_s"] == 8.0  # 1000 ms to 9000 ms


def test_video_that_never_ended_has_no_watch_time() -> None:
    s = summarize_events(
        records(
            ev(1, "video_load", video_id="1", duration=3.0, width=1, height=1, rect={}),
            ev(2, "video_play", video_id="1", media_time=0.0),
        )
    )
    (video,) = s["videos"]
    assert video["played"] and not video["ended"] and video["watch_s"] is None


def test_session_without_video_or_dots() -> None:
    s = summarize_events(records(ev(10, "session_start", client_info={})))
    assert s["videos"] == []
    assert s["dots"] == {
        "calibration": {"shown": 0, "clicked": 0},
        "validation": {"shown": 0, "clicked": 0},
    }
    assert s["duration_s"] == 0.0


def test_no_events_at_all() -> None:
    s = summarize_events([])
    assert s["duration_s"] is None and s["videos"] == []


def test_two_videos_keep_their_own_counts_in_order() -> None:
    s = summarize_events(
        records(
            ev(1, "video_play", video_id="2", media_time=0),
            ev(2, "video_pause", video_id="2", media_time=0),
            ev(3, "video_ended", video_id="2"),
            ev(4, "video_play", video_id="5", media_time=0),
            ev(5, "video_stall", video_id="5", media_time=0),
            ev(6, "video_stall", video_id="5", media_time=0),
            ev(9, "client_error", message="x", stack=None, url="u"),
        )
    )
    assert [(v["video_id"], v["pauses"], v["stalls"], v["ended"]) for v in s["videos"]] == [
        ("2", 1, 0, True),
        ("5", 0, 2, False),
    ]
    assert s["client_errors"] == 1


# --- the page and the API on real PostgreSQL and real chunk files --------------------------------


def seed(admin: Admin, engine: Engine, data_root: Path) -> dict[str, Any]:
    study = add_study(engine, "pstudy")
    other_study = add_study(engine, "other")
    complete = add_session(engine, study, "AB0315", 0, status=SessionStatus.UPLOADED)
    add_chunk(
        engine,
        data_root,
        study,
        complete,
        "events",
        0,
        gz(
            ev(0, "session_start", client_info={}),
            *dots("calibration", 13, 13, 100),
            ev(2000, "video_play", video_id="3", media_time=0),
        ),
    )
    add_chunk(
        engine, data_root, study, complete, "events", 1, gz(ev(5000, "video_ended", video_id="3"))
    )
    # Same participant again: interrupted, with a hole at seq 1 and few events.
    interrupted = add_session(
        engine, study, "AB0315", 10, status=SessionStatus.UPLOADED, flags=["interrupted"]
    )
    add_chunk(engine, data_root, study, interrupted, "events", 0, gz(ev(0, "session_start")))
    add_chunk(engine, data_root, study, interrupted, "events", 2, gz(ev(9000, "window_blur")))
    # Someone else in the same study, and the same code in another study: never mixed in.
    add_session(engine, study, "ZZ1231", 20)
    add_session(engine, other_study, "AB0315", 30)
    return {"study": study, "complete": complete, "interrupted": interrupted}


def test_results_of_one_participant(admin: Admin, migrated_engine: Engine, data_root: Path) -> None:
    ids = seed(admin, migrated_engine, data_root)
    r = admin.client.get(f"/admin/api/participants/{ids['study']}/AB0315")
    assert r.status_code == 200
    body = r.json()
    assert body["participant_code"] == "AB0315" and body["study"]["code"] == "pstudy"
    by_id = {s["id"]: s for s in body["sessions"]}
    assert set(by_id) == {str(ids["complete"]), str(ids["interrupted"])}

    done = by_id[str(ids["complete"])]
    assert done["status"] == "uploaded" and done["chunks"]["events"] == 2
    assert done["events_error"] is None
    assert done["summary"]["dots"]["calibration"] == {"shown": 13, "clicked": 13}
    (video,) = done["summary"]["videos"]
    assert video["video_id"] == "3" and video["ended"] and video["watch_s"] == 3.0

    broken = by_id[str(ids["interrupted"])]
    assert broken["flags"] == ["interrupted"]
    assert broken["summary"]["duration_s"] == 9.0  # what exists is still summarized
    assert broken["missing_event_chunks"] == [1]  # the gap is reported, not hidden


def test_code_is_matched_case_insensitively_and_unknown_code_is_empty(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    ids = seed(admin, migrated_engine, data_root)
    lower = admin.client.get(f"/admin/api/participants/{ids['study']}/ab0315").json()
    assert len(lower["sessions"]) == 2
    nobody = admin.client.get(f"/admin/api/participants/{ids['study']}/XX0101")
    assert nobody.status_code == 200 and nobody.json()["sessions"] == []


def test_unknown_study_is_404(admin: Admin) -> None:
    assert admin.client.get("/admin/api/participants/999/AB0315").status_code == 404
    assert admin.client.get("/admin/participants/999/AB0315").status_code == 404


def test_page_shows_summary_links_and_no_javascript(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    ids = seed(admin, migrated_engine, data_root)
    page = admin.client.get(f"/admin/participants/{ids['study']}/AB0315")
    assert page.status_code == 200 and "<script" not in page.text
    assert f"/admin/sessions/{ids['complete']}" in page.text
    assert "interrupted" in page.text
    # The sessions list links to the participant page.
    listing = admin.client.get(f"/admin/sessions?study_id={ids['study']}").text
    assert f"/admin/participants/{ids['study']}/AB0315" in listing


def test_anonymous_access_is_refused(migrated_engine: Engine, data_root: Path) -> None:
    with anonymous_client(migrated_engine, data_root) as client:
        assert client.get("/admin/api/participants/1/AB0315").status_code == 401
        assert client.get("/admin/participants/1/AB0315").status_code == 303
