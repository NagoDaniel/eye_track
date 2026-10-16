import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, update
from sqlalchemy.orm import Session as Db

from app.models import Session as SessionRow
from app.models import SessionStatus, Study, Video
from app.ratelimit import SlidingWindowLimiter
from app.schemas import SessionCreateResponse, StudyConfig
from tests.adminutil import logged_in_admin
from tests.dbutil import make_settings
from tests.participant.helpers import (
    PARTICIPANT,
    STUDY_CODE,
    MakeClient,
    auth,
    consent,
    create_session,
)

# --- study info ---------------------------------------------------------------------------------


def test_study_info(client: TestClient, study_id: int) -> None:
    r = client.get(f"/api/studies/{STUDY_CODE}")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Physics lecture study"
    assert body["code"] == STUDY_CODE and body["mode"] == "remote"
    assert body["consent_version"] == "v1"
    assert "webcam video is recorded" in body["consent_text"]
    assert "getUserMedia" in body["browser_requirements"]


def test_unknown_study_is_404(client: TestClient, study_id: int) -> None:
    assert client.get("/api/studies/nope").status_code == 404
    r = client.post("/api/sessions", json={"study_code": "nope", "participant_code": PARTICIPANT})
    assert r.status_code == 404


# --- session creation ---------------------------------------------------------------------------


def test_create_session_response_matches_schema_and_playlist_order(
    client: TestClient, study_id: int, migrated_engine: Engine
) -> None:
    created = create_session(client)
    parsed = SessionCreateResponse.model_validate(created)
    assert uuid.UUID(parsed.session_id)
    with Db(migrated_engine) as db:
        v1, v2 = (db.get(Video, i) for i in (1, 2))
        assert v1 is not None and v2 is not None
    # playlist is [v2, v1]: the response keeps the config order
    assert [v.id for v in parsed.videos] == [str(v2.id), str(v1.id)]
    assert parsed.videos[0].url == f"/media/videos/{v2.id}.mp4"
    assert parsed.videos[0].sha256 == "b" * 64 and parsed.videos[0].duration == 20.0
    assert parsed.videos[1].duration == 10.5
    assert [v.fps for v in parsed.videos] == [30, 25]  # the video's own frame rate, in playlist order
    assert parsed.config.playlist == [str(v2.id), str(v1.id)]


def test_session_row_defaults(client: TestClient, study_id: int, migrated_engine: Engine) -> None:
    created = create_session(client)
    with Db(migrated_engine) as db:
        row = db.get(SessionRow, uuid.UUID(created["session_id"]))
        assert row is not None
        assert row.status == SessionStatus.CREATED
        assert row.is_debug is False and row.flags == []
        assert row.participant_code == PARTICIPANT and row.study_id == study_id


def test_participant_code_is_normalized_and_validated(client: TestClient, study_id: int) -> None:
    r = client.post(
        "/api/sessions", json={"study_code": STUDY_CODE, "participant_code": " ab0315 "}
    )
    assert r.status_code == 201
    for bad in ["A0315", "AB1315", "AB0332", "ABC315", "", "ÁB0315"]:
        r = client.post("/api/sessions", json={"study_code": STUDY_CODE, "participant_code": bad})
        assert r.status_code == 422, bad


def test_token_is_stored_only_as_sha256(
    client: TestClient, study_id: int, migrated_engine: Engine
) -> None:
    created = create_session(client)
    token = created["upload_token"]
    assert len(token) >= 43  # token_urlsafe(32)
    with Db(migrated_engine) as db:
        row = db.get(SessionRow, uuid.UUID(created["session_id"]))
        assert row is not None
        assert row.upload_token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in row.upload_token_hash


def test_meta_json_holds_config_snapshot(
    client: TestClient, study_id: int, data_root: Path
) -> None:
    created = create_session(client)
    sid = created["session_id"]
    meta_file = data_root / "studies" / str(study_id) / "sessions" / sid / "raw" / "meta.json"
    meta: dict[str, Any] = json.loads(meta_file.read_text())
    assert meta["id"] == sid and meta["study_id"] == study_id
    assert meta["participant_code"] == PARTICIPANT and meta["status"] == "created"
    assert "created_at" in meta
    assert meta["config"] == json.loads(
        StudyConfig.model_validate(created["config"]).model_dump_json()
    )
    assert not list(meta_file.parent.glob("*.tmp")) and not list(meta_file.parent.glob(".*.tmp"))


def test_playlist_video_without_row_is_a_loud_500(
    client: TestClient, study_id: int, migrated_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    with Db(migrated_engine) as db:
        db.execute(update(Video).where(Video.id == 1).values(id=99))
        db.commit()
    with caplog.at_level("ERROR"):
        r = client.post(
            "/api/sessions", json={"study_code": STUDY_CODE, "participant_code": PARTICIPANT}
        )
    assert r.status_code == 500
    assert any("does not exist" in rec.getMessage() for rec in caplog.records)


# --- token checks -------------------------------------------------------------------------------


def consent_request(
    client: TestClient, session_id: str, headers: dict[str, str] | None = None, version: str = "v1"
) -> Any:
    return client.post(
        f"/api/sessions/{session_id}/consent", json={"consent_version": version}, headers=headers
    )


def test_missing_wrong_malformed_and_other_session_tokens_are_401(
    client: TestClient, study_id: int
) -> None:
    a, b = create_session(client), create_session(client)
    sid = a["session_id"]
    assert consent_request(client, sid).status_code == 401
    assert consent_request(client, sid, {"Authorization": "Bearer nope"}).status_code == 401
    assert consent_request(client, sid, {"Authorization": "Bearer "}).status_code == 401
    assert consent_request(client, sid, {"Authorization": a["upload_token"]}).status_code == 401
    assert consent_request(client, sid, {"Authorization": "Basic abc"}).status_code == 401
    assert consent_request(client, sid, auth(b)).status_code == 401  # other session's token
    assert consent_request(client, "not-a-uuid", auth(a)).status_code == 401
    assert consent_request(client, str(uuid.uuid4()), auth(a)).status_code == 401
    assert consent_request(client, sid, auth(a)).status_code == 200


def test_token_older_than_max_age_is_401_by_setting(make_client: MakeClient, study_id: int) -> None:
    client = make_client(upload_token_max_age_s=0)
    created = create_session(client)
    assert consent_request(client, created["session_id"], auth(created)).status_code == 401


def test_token_older_than_24_hours_is_401_by_backdating(
    client: TestClient, study_id: int, migrated_engine: Engine
) -> None:
    created = create_session(client)
    sid = uuid.UUID(created["session_id"])
    with Db(migrated_engine) as db:
        db.execute(
            update(SessionRow)
            .where(SessionRow.id == sid)
            .values(created_at=datetime.now(UTC) - timedelta(hours=24, minutes=1))
        )
        db.commit()
    assert consent_request(client, created["session_id"], auth(created)).status_code == 401


def test_default_token_age_is_24_hours() -> None:
    assert make_settings().upload_token_max_age_s == 24 * 3600


# --- consent ------------------------------------------------------------------------------------


def test_consent_sets_version_time_and_status(
    client: TestClient, study_id: int, migrated_engine: Engine
) -> None:
    created = create_session(client)
    before = datetime.now(UTC)
    r = consent_request(client, created["session_id"], auth(created))
    assert r.status_code == 200 and r.json() == {"status": "consented"}
    with Db(migrated_engine) as db:
        row = db.get(SessionRow, uuid.UUID(created["session_id"]))
        assert row is not None
        assert row.status == SessionStatus.CONSENTED and row.consent_version == "v1"
        assert row.consent_at is not None and before <= row.consent_at <= datetime.now(UTC)


def test_repeated_consent_is_a_no_op(
    client: TestClient, study_id: int, migrated_engine: Engine
) -> None:
    created = create_session(client)
    consent(client, created)
    with Db(migrated_engine) as db:
        first = db.get(SessionRow, uuid.UUID(created["session_id"]))
        assert first is not None
        stamp = first.consent_at
    r = consent_request(client, created["session_id"], auth(created))
    assert r.status_code == 200 and r.json() == {"status": "consented"}
    with Db(migrated_engine) as db:
        again = db.get(SessionRow, uuid.UUID(created["session_id"]))
        assert again is not None and again.consent_at == stamp


def test_wrong_consent_version_is_409_and_changes_nothing(
    client: TestClient, study_id: int, migrated_engine: Engine
) -> None:
    created = create_session(client)
    r = consent_request(client, created["session_id"], auth(created), version="v0")
    assert r.status_code == 409
    with Db(migrated_engine) as db:
        row = db.get(SessionRow, uuid.UUID(created["session_id"]))
        assert row is not None
        assert row.status == SessionStatus.CREATED and row.consent_at is None


# --- rate limit ---------------------------------------------------------------------------------


def test_session_creation_is_limited_per_ip(make_client: MakeClient, study_id: int) -> None:
    client = make_client(rate_limit_per_hour=3)
    body = {"study_code": STUDY_CODE, "participant_code": PARTICIPANT}
    assert [client.post("/api/sessions", json=body).status_code for _ in range(3)] == [201] * 3
    assert client.post("/api/sessions", json=body).status_code == 429
    # another client address has its own window (rightmost X-Forwarded-For entry wins)
    other = {"X-Forwarded-For": "198.51.100.7"}
    assert client.post("/api/sessions", json=body, headers=other).status_code == 201
    # a spoofed leftmost entry does not help: the proxy-appended rightmost one is used
    spoof = {"X-Forwarded-For": "203.0.113.9, testclient"}
    assert client.post("/api/sessions", json=body, headers=spoof).status_code == 429


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limiter_window_slides_with_injected_clock() -> None:
    clock = FakeClock()
    limiter = SlidingWindowLimiter(2, window_s=60, clock=clock)
    assert limiter.allow("k") and limiter.allow("k") and not limiter.allow("k")
    assert limiter.allow("other")
    clock.now += 59
    assert not limiter.allow("k")
    clock.now += 2
    assert limiter.allow("k")
    clock.now += 3600  # idle keys are swept
    assert limiter.allow("fresh")
    assert set(limiter._hits) == {"fresh"}


def test_default_rate_limit_is_300_per_hour() -> None:
    assert make_settings().rate_limit_per_hour == 300


# --- debug sessions (PLAN §12.1) ----------------------------------------------------------------


def session_count(engine: Engine) -> int:
    with Db(engine) as db:
        return len(db.scalars(select(SessionRow)).all())


def allow_debug(engine: Engine, study_id: int) -> None:
    with Db(engine) as db:
        study = db.get(Study, study_id)
        assert study is not None
        study.config = {**study.config, "debug_allowed": True}
        db.commit()


def request_debug(client: TestClient) -> Any:
    return client.post(
        "/api/sessions",
        json={"study_code": STUDY_CODE, "participant_code": PARTICIPANT, "debug": True},
    )


def test_normal_session_is_not_a_debug_session(
    client: TestClient, study_id: int, migrated_engine: Engine
) -> None:
    created = create_session(client)
    with Db(migrated_engine) as db:
        row = db.get(SessionRow, uuid.UUID(created["session_id"]))
        assert row is not None and row.is_debug is False


def test_debug_session_is_refused_when_the_study_does_not_allow_it(
    client: TestClient, study_id: int, migrated_engine: Engine
) -> None:
    r = request_debug(client)
    assert r.status_code == 403 and "debug" in r.json()["detail"]
    assert session_count(migrated_engine) == 0  # no row, no files


def test_debug_session_is_created_when_the_study_allows_it(
    client: TestClient, study_id: int, migrated_engine: Engine, data_root: Path
) -> None:
    allow_debug(migrated_engine, study_id)
    r = request_debug(client)
    assert r.status_code == 201, r.text
    sid = r.json()["session_id"]
    with Db(migrated_engine) as db:
        row = db.get(SessionRow, uuid.UUID(sid))
        assert row is not None and row.is_debug is True
    raw = data_root / "studies" / str(study_id) / "sessions" / sid / "raw"
    assert json.loads((raw / "meta.json").read_text())["is_debug"] is True


def test_a_logged_in_admin_may_start_a_debug_session(
    migrated_engine: Engine, data_root: Path, study_id: int
) -> None:
    with logged_in_admin(migrated_engine, data_root) as admin:
        r = request_debug(admin.client)
        assert r.status_code == 201, r.text
        with Db(migrated_engine) as db:
            row = db.get(SessionRow, uuid.UUID(r.json()["session_id"]))
            assert row is not None and row.is_debug is True


def test_debug_is_refused_for_a_client_with_a_forged_admin_cookie(
    client: TestClient, study_id: int, migrated_engine: Engine
) -> None:
    client.cookies.set("eyetrack_admin", "forged.value")
    assert request_debug(client).status_code == 403
    assert session_count(migrated_engine) == 0
