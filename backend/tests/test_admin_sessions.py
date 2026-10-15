"""Admin session list and detail on real PostgreSQL and real chunk files (PLAN §6)."""

import gzip
import hashlib
import json
import uuid
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as DbSession

from app.models import Chunk, ChunkStream, Session, SessionStatus, Study, StudyMode
from app.schemas import Stream
from app.sessions_view import MAX_EVENTS_SHOWN
from app.storage import chunk_path
from tests.adminutil import Admin, anonymous_client, logged_in_admin

BASE_TIME = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def admin(migrated_engine: Engine, data_root: Path) -> Generator[Admin]:
    with logged_in_admin(migrated_engine, data_root) as a:
        yield a


def add_study(engine: Engine, code: str) -> int:
    with DbSession(engine) as db:
        study = Study(name=f"Study {code}", code=code, mode=StudyMode.REMOTE, config={})
        db.add(study)
        db.commit()
        return study.id


def add_session(
    engine: Engine, study_id: int, participant: str, minutes: int = 0, **fields: Any
) -> uuid.UUID:
    with DbSession(engine) as db:
        session = Session(
            study_id=study_id,
            participant_code=participant,
            upload_token_hash="x",
            created_at=BASE_TIME + timedelta(minutes=minutes),
            **fields,
        )
        db.add(session)
        db.commit()
        return session.id


def gz(*events: dict[str, Any]) -> bytes:
    return gzip.compress("".join(json.dumps(e) + "\n" for e in events).encode())


def add_chunk(
    engine: Engine,
    data_root: Path,
    study_id: int,
    session_id: uuid.UUID,
    stream: Stream,
    seq: int,
    payload: bytes | None = None,
) -> None:
    """A chunk row, and its file unless `payload` is None for a stream whose bytes do not matter."""
    data = payload if payload is not None else b"x" * (seq + 1)
    path = chunk_path(data_root, study_id, session_id, stream, seq)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    with DbSession(engine) as db:
        db.add(
            Chunk(
                session_id=session_id,
                stream=ChunkStream(stream),
                seq=seq,
                size=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        )
        db.commit()


def ev(t: float, type_: str = "video_play", **data: Any) -> dict[str, Any]:
    return {"t": t, "type": type_, "data": data}


def test_list_filters_by_study_and_counts_chunks(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    s1, s2 = add_study(migrated_engine, "st1"), add_study(migrated_engine, "st2")
    old = add_session(migrated_engine, s1, "AB0315", minutes=0, status=SessionStatus.UPLOADED)
    new = add_session(migrated_engine, s1, "CD0101", minutes=5, flags=["interrupted"])
    other = add_session(migrated_engine, s2, "EF1231", minutes=10)
    for seq in range(3):
        add_chunk(migrated_engine, data_root, s1, old, "webcam", seq)
    add_chunk(migrated_engine, data_root, s1, old, "events", 0, gz(ev(1)))
    add_chunk(migrated_engine, data_root, s2, other, "features", 0)

    rows = admin.client.get("/admin/api/sessions").json()
    assert [r["id"] for r in rows] == [str(other), str(new), str(old)]  # newest first

    rows = admin.client.get("/admin/api/sessions", params={"study_id": s1}).json()
    assert [r["id"] for r in rows] == [str(new), str(old)]
    by_id = {r["id"]: r for r in rows}
    assert by_id[str(old)]["chunks"] == {
        "webcam": 3,
        "features": 0,
        "events": 1,
        "stimulus_frames": 0,
    }
    assert by_id[str(old)]["status"] == "uploaded"
    assert by_id[str(new)]["flags"] == ["interrupted"]
    assert by_id[str(new)]["chunks"] == {
        "webcam": 0,
        "features": 0,
        "events": 0,
        "stimulus_frames": 0,
    }
    assert (by_id[str(old)]["participant_code"], by_id[str(old)]["study_code"]) == ("AB0315", "st1")
    assert by_id[str(old)]["created_at"].startswith("2026-10-01T12:00")

    rows = admin.client.get("/admin/api/sessions", params={"study_id": s2}).json()
    assert [r["id"] for r in rows] == [str(other)] and rows[0]["chunks"]["features"] == 1
    assert admin.client.get("/admin/api/sessions", params={"study_id": 999}).json() == []


def test_detail_shows_events_in_order_and_a_missing_seq_as_a_gap(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    study = add_study(migrated_engine, "gap1")
    sid = add_session(migrated_engine, study, "AB0315")
    # Inserted out of order on purpose; seq 2 never arrived.
    add_chunk(migrated_engine, data_root, study, sid, "events", 3, gz(ev(40.0, "video_ended")))
    add_chunk(migrated_engine, data_root, study, sid, "events", 0, gz(ev(1.0, "session_start")))
    add_chunk(
        migrated_engine, data_root, study, sid, "events", 1, gz(ev(2.0), ev(3.0, "video_pause"))
    )
    for seq in (0, 5):
        add_chunk(migrated_engine, data_root, study, sid, "webcam", seq)

    d = admin.client.get(f"/admin/api/sessions/{sid}").json()
    assert d["streams"]["events"] == {
        "count": 3,
        "highest_seq": 3,
        "missing": [2],
        "total_bytes": sum(
            chunk_path(data_root, study, sid, "events", n).stat().st_size for n in (0, 1, 3)
        ),
    }
    assert d["streams"]["webcam"]["missing"] == [1, 2, 3, 4]
    assert d["streams"]["webcam"]["total_bytes"] == 1 + 6
    assert d["streams"]["features"] == {
        "count": 0, "highest_seq": None, "missing": [], "total_bytes": 0
    }  # fmt: skip
    assert d["chunks"] == {"webcam": 2, "features": 0, "events": 3, "stimulus_frames": 0}
    assert [(e["t"], e["type"]) for e in d["events"]] == [
        (1.0, "session_start"),
        (2.0, "video_play"),
        (3.0, "video_pause"),
        (40.0, "video_ended"),
    ]
    assert d["events_total"] == 4 and d["events_error"] is None
    assert d["study_code"] == "gap1" and d["participant_code"] == "AB0315"


def test_at_most_1000_events_are_returned_with_the_total(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    study = add_study(migrated_engine, "many1")
    sid = add_session(migrated_engine, study, "AB0315")
    n_total = MAX_EVENTS_SHOWN + 500
    events = [ev(float(i)) for i in range(n_total)]
    for seq in range(3):
        add_chunk(
            migrated_engine,
            data_root,
            study,
            sid,
            "events",
            seq,
            gz(*events[seq * 500 : seq * 500 + 500]),
        )
    d = admin.client.get(f"/admin/api/sessions/{sid}").json()
    assert len(d["events"]) == MAX_EVENTS_SHOWN == 1000
    assert d["events_total"] == n_total
    assert [e["t"] for e in d["events"]] == [float(i) for i in range(MAX_EVENTS_SHOWN)]


def test_unreadable_events_are_reported_but_the_rest_is_shown(
    admin: Admin, migrated_engine: Engine, data_root: Path
) -> None:
    study = add_study(migrated_engine, "bad1")
    sid = add_session(migrated_engine, study, "AB0315")
    add_chunk(migrated_engine, data_root, study, sid, "events", 0, b"not gzip")
    r = admin.client.get(f"/admin/api/sessions/{sid}")
    assert r.status_code == 200
    d = r.json()
    assert d["events"] == [] and "not valid gzip" in d["events_error"]
    assert d["streams"]["events"]["count"] == 1

    chunk_path(data_root, study, sid, "events", 0).unlink()  # row without file
    assert "events_error" in admin.client.get(f"/admin/api/sessions/{sid}").json()
    assert admin.client.get(f"/admin/api/sessions/{sid}").json()["events_error"]


def test_unknown_session_is_404(admin: Admin) -> None:
    assert admin.client.get(f"/admin/api/sessions/{uuid.uuid4()}").status_code == 404
    assert admin.client.get(f"/admin/sessions/{uuid.uuid4()}").status_code == 404


def test_sessions_are_read_only(admin: Admin, migrated_engine: Engine) -> None:
    sid = add_session(migrated_engine, add_study(migrated_engine, "ro1"), "AB0315")
    c, h = admin.client, admin.headers
    for url in ("/admin/api/sessions", f"/admin/api/sessions/{sid}"):
        assert c.post(url, json={}, headers=h).status_code == 405
        assert c.put(url, json={}, headers=h).status_code == 405
        assert c.delete(url, headers=h).status_code == 405


def test_anonymous_gets_401_and_pages_redirect(migrated_engine: Engine, data_root: Path) -> None:
    sid = uuid.uuid4()
    with anonymous_client(migrated_engine, data_root) as c:
        assert c.get("/admin/api/sessions").status_code == 401
        assert c.get(f"/admin/api/sessions/{sid}").status_code == 401
        for path in ("/admin/sessions", f"/admin/sessions/{sid}"):
            r = c.get(path)
            assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_pages(admin: Admin, migrated_engine: Engine, data_root: Path) -> None:
    s1, s2 = add_study(migrated_engine, "pg1"), add_study(migrated_engine, "pg2")
    sid = add_session(migrated_engine, s1, "AB0315", flags=["calibration_gate_failed"])
    add_session(migrated_engine, s2, "ZZ1212")
    add_chunk(migrated_engine, data_root, s1, sid, "events", 0, gz(ev(1.5, "session_start")))
    add_chunk(migrated_engine, data_root, s1, sid, "events", 2, gz(ev(9.5, "video_ended", id="1")))

    page = admin.client.get("/admin/sessions")
    assert page.status_code == 200 and "<script" not in page.text
    assert "AB0315" in page.text and "ZZ1212" in page.text
    filtered = admin.client.get("/admin/sessions", params={"study_id": s1})
    assert "AB0315" in filtered.text and "ZZ1212" not in filtered.text
    assert "AB0315" in admin.client.get("/admin/sessions", params={"study_id": ""}).text

    detail = admin.client.get(f"/admin/sessions/{sid}")
    assert detail.status_code == 200
    assert "calibration_gate_failed" in detail.text
    assert "session_start" in detail.text and "video_ended" in detail.text
    assert "Events chunks 1 are missing" in detail.text  # the gap is visible
    assert 'class="gap"' in detail.text
