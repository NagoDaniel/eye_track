import gzip
import hashlib
import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session as Db

from app.chunks import ChunkConflict, store_chunk
from app.models import Chunk, ChunkStream, Job, SessionStatus
from app.models import Session as SessionRow
from app.settings import Settings
from tests.dbutil import make_settings
from tests.participant.helpers import MakeClient, auth, consent, create_session

CLIENT_INFO = {"user_agent": "TestBrowser/1.0", "screen": {"w": 1920, "h": 1080}}
EXAMPLES: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[3] / "shared" / "protocol.json").read_text()
)["examples"]
FEATURE = EXAMPLES["feature_record"]
NO_FACE = EXAMPLES["feature_record_no_face"]
FRAME = EXAMPLES["stimulus_frame_record"]
WEBM = b"\x1a\x45\xdf\xa3"  # the EBML magic every WebM file starts with


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put(
    client: TestClient,
    created: dict[str, Any],
    stream: str,
    seq: int | str,
    body: bytes,
    digest: str | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    hdrs = {**auth(created), "X-Chunk-SHA256": digest if digest is not None else sha(body)}
    hdrs.update(headers or {})
    return client.put(
        f"/api/sessions/{created['session_id']}/chunks/{stream}/{seq}", content=body, headers=hdrs
    )


def complete(
    client: TestClient, created: dict[str, Any], last_seq: dict[str, int], **extra: Any
) -> Response:
    return client.post(
        f"/api/sessions/{created['session_id']}/complete",
        json={"last_seq": last_seq, **extra},
        headers=auth(created),
    )


def events_chunk(*events: dict[str, Any]) -> bytes:
    return gzip.compress("".join(json.dumps(e) + "\n" for e in events).encode())


def ev(t: float, type_: str, **data: Any) -> dict[str, Any]:
    return {"t": t, "type": type_, "data": data}


def session_dir(data_root: Path, study_id: int, created: dict[str, Any]) -> Path:
    return data_root / "studies" / str(study_id) / "sessions" / str(created["session_id"])


def chunk_file(
    data_root: Path, study_id: int, created: dict[str, Any], stream: str, seq: int
) -> Path:
    return session_dir(data_root, study_id, created) / "raw" / "chunks" / stream / f"{seq:06d}.bin"


def status_of(engine: Engine, created: dict[str, Any]) -> SessionStatus:
    with Db(engine) as db:
        row = db.get(SessionRow, uuid.UUID(created["session_id"]))
        assert row is not None
        return row.status


def chunk_count(engine: Engine, created: dict[str, Any]) -> int:
    with Db(engine) as db:
        n = db.scalar(
            select(func.count())
            .select_from(Chunk)
            .where(Chunk.session_id == uuid.UUID(created["session_id"]))
        )
        return n or 0


@pytest.fixture
def created(client: TestClient, study_id: int) -> dict[str, Any]:
    """A consented session."""
    c = create_session(client)
    consent(client, c)
    return c


def test_default_limits() -> None:
    s = make_settings()
    assert s.max_upload_chunk_mb == 10 and s.max_session_total_mb == 2048


# --- PUT ----------------------------------------------------------------------------------------


def test_put_stores_file_row_and_acks(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    body = WEBM + b"webm bytes" * 100
    r = put(client, created, "webcam", 0, body)
    assert r.status_code == 200
    assert r.json() == {"stream": "webcam", "seq": 0, "size": len(body)}
    assert chunk_file(data_root, study_id, created, "webcam", 0).read_bytes() == body
    with Db(migrated_engine) as db:
        row = db.get(Chunk, (uuid.UUID(created["session_id"]), ChunkStream.WEBCAM, 0))
        assert row is not None and row.sha256 == sha(body) and row.size == len(body)


def test_first_chunk_flips_status_to_recording(
    client: TestClient, created: dict[str, Any], migrated_engine: Engine
) -> None:
    assert status_of(migrated_engine, created) == SessionStatus.CONSENTED
    assert put(client, created, "features", 0, events_chunk(FEATURE)).status_code == 200
    assert status_of(migrated_engine, created) == SessionStatus.RECORDING
    assert put(client, created, "features", 1, events_chunk(NO_FACE)).status_code == 200
    assert status_of(migrated_engine, created) == SessionStatus.RECORDING


def test_repeat_put_is_idempotent_and_does_not_rewrite(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    body = gzip.compress(b"x" * 1000)
    first = put(client, created, "features", 3, body)
    path = chunk_file(data_root, study_id, created, "features", 3)
    stamp = path.stat().st_mtime_ns
    inode = path.stat().st_ino
    again = put(client, created, "features", 3, body)
    assert again.status_code == 200 and again.json() == first.json()
    assert path.stat().st_mtime_ns == stamp and path.stat().st_ino == inode
    assert chunk_count(migrated_engine, created) == 1


def test_same_seq_different_bytes_is_409_and_keeps_original(
    client: TestClient, created: dict[str, Any], data_root: Path, study_id: int
) -> None:
    assert put(client, created, "webcam", 0, WEBM + b"original").status_code == 200
    r = put(client, created, "webcam", 0, WEBM + b"different")
    assert r.status_code == 409
    assert chunk_file(data_root, study_id, created, "webcam", 0).read_bytes() == WEBM + b"original"


def test_hash_mismatch_and_missing_header(
    client: TestClient, created: dict[str, Any], migrated_engine: Engine
) -> None:
    assert put(client, created, "webcam", 0, b"data", digest=sha(b"other")).status_code == 400
    assert (
        put(client, created, "webcam", 0, b"data", digest=sha(b"data").upper()).status_code == 400
    )
    r = client.put(
        f"/api/sessions/{created['session_id']}/chunks/webcam/0",
        content=b"data",
        headers=auth(created),
    )
    assert r.status_code == 422
    assert chunk_count(migrated_engine, created) == 0


def test_empty_body_is_400(client: TestClient, created: dict[str, Any]) -> None:
    assert put(client, created, "webcam", 0, b"").status_code == 400


@pytest.mark.parametrize(
    ("stream", "seq"),
    [("video", 0), ("WEBCAM", 0), ("webcam", -1), ("webcam", "x"), ("webcam", 2**31)],
)
def test_bad_stream_or_seq_is_422(
    client: TestClient, created: dict[str, Any], stream: str, seq: int | str
) -> None:
    assert put(client, created, stream, seq, b"data").status_code == 422


def test_oversize_chunk_is_413(
    make_client: MakeClient, study_id: int, migrated_engine: Engine
) -> None:
    client = make_client(max_upload_chunk_mb=1)
    c = create_session(client)
    consent(client, c)
    limit = 1024 * 1024
    assert put(client, c, "webcam", 0, b"x" * (limit + 1)).status_code == 413
    assert chunk_count(migrated_engine, c) == 0
    assert put(client, c, "webcam", 0, WEBM + b"x" * (limit - 4)).status_code == 200


def test_session_total_limit_is_413_but_repeats_still_ack(
    make_client: MakeClient, study_id: int
) -> None:
    client = make_client(max_upload_chunk_mb=1, max_session_total_mb=1)
    c = create_session(client)
    consent(client, c)
    part = WEBM + b"y" * 600_000
    assert put(client, c, "webcam", 0, part).status_code == 200
    assert put(client, c, "webcam", 1, part).status_code == 413
    assert put(client, c, "webcam", 0, part).status_code == 200  # a repeat adds nothing
    assert put(client, c, "features", 0, events_chunk(FEATURE)).status_code == 200  # fits


def test_put_before_consent_and_after_complete_is_409(
    client: TestClient, study_id: int, migrated_engine: Engine
) -> None:
    c = create_session(client)
    r = put(client, c, "events", 0, events_chunk(ev(0, "session_start")))
    assert r.status_code == 409 and "consent" in r.json()["detail"]
    assert chunk_count(migrated_engine, c) == 0
    consent(client, c)
    assert put(client, c, "events", 0, events_chunk(ev(0, "session_start"))).status_code == 200
    assert complete(client, c, {"events": 0}).json()["status"] == "uploaded"
    r = put(client, c, "events", 1, b"late")
    assert r.status_code == 409 and "completed" in r.json()["detail"]


def test_put_needs_a_valid_token(client: TestClient, created: dict[str, Any]) -> None:
    r = client.put(
        f"/api/sessions/{created['session_id']}/chunks/webcam/0",
        content=b"d",
        headers={"X-Chunk-SHA256": sha(b"d")},
    )
    assert r.status_code == 401
    r = client.put(
        f"/api/sessions/{created['session_id']}/chunks/webcam/0",
        content=b"d",
        headers={"X-Chunk-SHA256": sha(b"d"), "Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_concurrent_identical_puts_leave_one_row_and_one_file(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    body = b"z" * 200_000
    barrier = threading.Barrier(12)

    def send(_: int) -> Response:
        barrier.wait()
        return put(client, created, "webcam", 5, body)

    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(send, range(12)))
    assert {r.status_code for r in results} == {200}
    assert {json.dumps(r.json(), sort_keys=True) for r in results} == {
        json.dumps({"stream": "webcam", "seq": 5, "size": len(body)}, sort_keys=True)
    }
    assert chunk_count(migrated_engine, created) == 1
    chunk_dir = chunk_file(data_root, study_id, created, "webcam", 5).parent
    assert [p.name for p in chunk_dir.iterdir()] == ["000005.bin"]
    assert (chunk_dir / "000005.bin").read_bytes() == body


def _store(engine: Engine, settings: Settings, session_id: uuid.UUID, body: bytes) -> object:
    with Db(engine) as db:
        row = db.get(SessionRow, session_id)
        assert row is not None
        try:
            return store_chunk(db, settings.data_root, row, "webcam", 0, body, sha(body), 10**9)
        except ChunkConflict as exc:
            return exc


@pytest.mark.parametrize("same_bytes", [True, False])
def test_loser_of_an_insert_race_rereads_the_winner(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
    same_bytes: bool,
) -> None:
    """Force the unique-violation path: the winner's insert is flushed but not yet committed."""
    sid = uuid.UUID(created["session_id"])
    winner_body = b"winner"
    loser_body = winner_body if same_bytes else b"loser!"
    settings = make_settings(data_root=data_root)
    path = chunk_file(data_root, study_id, created, "webcam", 0)
    result: list[object] = []

    with Db(migrated_engine) as winner:
        winner.add(
            Chunk(
                session_id=sid,
                stream=ChunkStream.WEBCAM,
                seq=0,
                size=len(winner_body),
                sha256=sha(winner_body),
            )
        )
        winner.flush()
        loser = threading.Thread(
            target=lambda: result.append(_store(migrated_engine, settings, sid, loser_body))
        )
        loser.start()
        time.sleep(1.0)  # the loser is now blocked on the winner's uncommitted unique key
        assert loser.is_alive() and not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(winner_body)
        winner.commit()
    loser.join(timeout=10)
    assert not loser.is_alive()
    if same_bytes:
        assert getattr(result[0], "size", None) == len(winner_body)
    else:
        assert isinstance(result[0], ChunkConflict)
    assert path.read_bytes() == winner_body
    assert chunk_count(migrated_engine, created) == 1


# --- complete -----------------------------------------------------------------------------------


def upload_all(client: TestClient, created: dict[str, Any]) -> dict[str, list[bytes]]:
    """Events (3 gzip chunks, out of order) plus webcam and features chunks."""
    chunks = {
        "events": [
            events_chunk(
                ev(0.0, "session_start", client_info=CLIENT_INFO), ev(0.5, "video_load", id="1")
            ),
            events_chunk(ev(1.0, "video_play", id="1")),
            events_chunk(ev(2.0, "video_pause", id="1"), ev(2.5, "video_ended", id="1")),
        ],
        "webcam": [WEBM + b"w0" * 50, b"w1" * 50],
        "features": [events_chunk(FEATURE, NO_FACE), events_chunk(FEATURE)],
        "stimulus_frames": [events_chunk(FRAME, FRAME), events_chunk(FRAME)],
    }
    order = [("events", 2), ("webcam", 1), ("events", 0), ("features", 0), ("webcam", 0)]
    order += [("events", 1), ("features", 1), ("stimulus_frames", 1), ("stimulus_frames", 0)]
    for stream, seq in order:
        assert put(client, created, stream, seq, chunks[stream][seq]).status_code == 200
    return chunks


LAST = {"events": 2, "webcam": 1, "features": 1, "stimulus_frames": 1}


def test_complete_detects_missing_then_succeeds_after_resend(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    ev_chunks = [events_chunk(ev(0.0, "session_start")), events_chunk(ev(1.0, "video_play"))]
    ev_chunks.append(events_chunk(ev(2.0, "video_ended")))
    for seq in (0, 2):
        put(client, created, "events", seq, ev_chunks[seq])
    put(client, created, "webcam", 1, b"w1")
    r = complete(client, created, {"events": 2, "webcam": 1})
    assert r.status_code == 200
    assert r.json() == {"status": "incomplete", "missing": {"events": [1], "webcam": [0]}}
    assert status_of(migrated_engine, created) == SessionStatus.RECORDING
    assert not (session_dir(data_root, study_id, created) / "raw" / "events.jsonl").exists()
    put(client, created, "events", 1, ev_chunks[1])
    put(client, created, "webcam", 0, WEBM + b"w0")
    r = complete(client, created, {"events": 2, "webcam": 1})
    assert r.json() == {"status": "uploaded", "missing": {}}
    assert status_of(migrated_engine, created) == SessionStatus.UPLOADED


def test_complete_assembles_events_jsonl_and_stores_client_info_and_meta(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    chunks = upload_all(client, created)
    raw = session_dir(data_root, study_id, created) / "raw"
    before = json.loads((raw / "meta.json").read_text())
    r = complete(client, created, LAST, interrupted=True)
    assert r.json() == {"status": "uploaded", "missing": {}}

    expected = b"".join(gzip.decompress(c) for c in chunks["events"])
    assert (raw / "events.jsonl").read_bytes() == expected
    assert [json.loads(line)["t"] for line in expected.splitlines()] == [0.0, 0.5, 1.0, 2.0, 2.5]

    with Db(migrated_engine) as db:
        row = db.get(SessionRow, uuid.UUID(created["session_id"]))
        assert row is not None
        assert row.status == SessionStatus.UPLOADED
        assert row.client_info == CLIENT_INFO
        assert row.flags == ["interrupted"]

    after = json.loads((raw / "meta.json").read_text())
    assert before["status"] == "created" and after["status"] == "uploaded"
    assert after["config"] == before["config"]
    assert after["flags"] == ["interrupted"] and after["client_info"] == CLIENT_INFO
    assert after["consent_version"] == "v1" and after["consent_at"] is not None
    assert after["id"] == created["session_id"]


def test_complete_without_interrupted_has_no_flag(
    client: TestClient, created: dict[str, Any], migrated_engine: Engine
) -> None:
    upload_all(client, created)
    assert complete(client, created, LAST).json()["status"] == "uploaded"
    with Db(migrated_engine) as db:
        row = db.get(SessionRow, uuid.UUID(created["session_id"]))
        assert row is not None and row.flags == []


def test_complete_is_idempotent(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    upload_all(client, created)
    first = complete(client, created, LAST, interrupted=True)
    raw = session_dir(data_root, study_id, created) / "raw"
    events_1, meta_1 = (
        (raw / "events.jsonl").read_bytes(),
        json.loads((raw / "meta.json").read_text()),
    )
    second = complete(client, created, LAST, interrupted=True)
    assert second.status_code == 200 and second.json() == first.json()
    assert (raw / "events.jsonl").read_bytes() == events_1
    assert json.loads((raw / "meta.json").read_text()) == meta_1
    assert meta_1["flags"] == ["interrupted"]
    # still reports gaps when asked about chunks that never arrived
    r = complete(client, created, {**LAST, "webcam": 3})
    assert r.json() == {"status": "incomplete", "missing": {"webcam": [2, 3]}}
    assert status_of(migrated_engine, created) == SessionStatus.UPLOADED


def test_complete_creates_no_job_and_leaves_no_tmp_files(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    upload_all(client, created)
    complete(client, created, LAST)
    with Db(migrated_engine) as db:
        assert db.scalar(select(func.count()).select_from(Job)) == 0
    assert list(session_dir(data_root, study_id, created).rglob("*.tmp")) == []


def test_corrupt_events_chunk_is_refused_at_the_put_and_not_stored(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    good = events_chunk(ev(0.0, "session_start"))
    corrupt_body = good[:12] + bytes([good[12] ^ 0xFF]) + good[13:]  # makes zlib.error
    for bad in (b"this is not gzip", corrupt_body, good[:-6], good + b"junk"):
        r = put(client, created, "events", 1, bad)
        assert r.status_code == 422 and "not valid gzip" in r.json()["detail"]
    assert chunk_count(migrated_engine, created) == 0
    assert not chunk_file(data_root, study_id, created, "events", 1).exists()


def test_gzip_bomb_is_refused_at_the_put(
    make_client: MakeClient, study_id: int, migrated_engine: Engine
) -> None:
    client = make_client(max_events_chunk_inflated_mb=1)
    created = create_session(client)
    consent(client, created)
    bomb = gzip.compress(b"\0" * (5 * 1024 * 1024))
    assert len(bomb) < 10_000
    r = put(client, created, "events", 0, bomb)
    assert r.status_code == 422 and "inflates to more than" in r.json()["detail"]
    assert chunk_count(migrated_engine, created) == 0


def test_complete_with_an_unknown_event_type_finishes_and_flags_the_session(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    put(client, created, "events", 0, events_chunk(ev(0.0, "session_start")))
    put(client, created, "events", 1, events_chunk(ev(1.0, "a_type_from_a_newer_frontend")))
    r = complete(client, created, {"events": 1})
    assert r.status_code == 200 and r.json()["status"] == "uploaded"
    assert status_of(migrated_engine, created) == SessionStatus.UPLOADED
    with Db(migrated_engine) as db:
        row = db.get_one(SessionRow, uuid.UUID(created["session_id"]))
        assert row.flags == ["events_invalid"]
    assert not (session_dir(data_root, study_id, created) / "raw" / "events.jsonl").exists()
    # The chunks themselves are kept, and the meta.json carries the flag.
    assert chunk_file(data_root, study_id, created, "events", 1).exists()
    meta = json.loads((session_dir(data_root, study_id, created) / "raw" / "meta.json").read_text())
    assert meta["flags"] == ["events_invalid"] and meta["status"] == "uploaded"
    # Completing again changes nothing (no duplicate flag).
    assert complete(client, created, {"events": 1}).status_code == 200
    with Db(migrated_engine) as db:
        assert db.get_one(SessionRow, uuid.UUID(created["session_id"])).flags == ["events_invalid"]


def test_complete_of_a_session_without_any_chunk_is_flagged_empty(
    client: TestClient, created: dict[str, Any], migrated_engine: Engine
) -> None:
    r = complete(client, created, {}, interrupted=True)
    assert r.status_code == 200 and r.json()["status"] == "uploaded"
    with Db(migrated_engine) as db:
        row = db.get_one(SessionRow, uuid.UUID(created["session_id"]))
        assert row.flags == ["interrupted", "empty"]


def test_a_session_with_events_is_not_flagged_empty(
    client: TestClient, created: dict[str, Any], migrated_engine: Engine
) -> None:
    put(client, created, "events", 0, events_chunk(ev(0.0, "session_start")))
    assert complete(client, created, {"events": 0}).status_code == 200
    with Db(migrated_engine) as db:
        assert db.get_one(SessionRow, uuid.UUID(created["session_id"])).flags == []


def test_complete_with_a_garbage_event_line_finishes_and_flags_the_session(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    put(client, created, "events", 0, gzip.compress(b"this is not json\n"))
    r = complete(client, created, {"events": 0})
    assert r.status_code == 200 and r.json()["status"] == "uploaded"
    with Db(migrated_engine) as db:
        row = db.get_one(SessionRow, uuid.UUID(created["session_id"]))
        assert row.flags == ["events_invalid"] and row.client_info is None
    assert not (session_dir(data_root, study_id, created) / "raw" / "events.jsonl").exists()
    assert any("line 1" in str(getattr(r, "error", "")) for r in caplog.records)  # reason logged


def test_complete_without_session_start_keeps_client_info_empty(
    client: TestClient, created: dict[str, Any], migrated_engine: Engine
) -> None:
    put(client, created, "events", 0, events_chunk(ev(0.0, "video_play")))
    assert complete(client, created, {"events": 0}).json()["status"] == "uploaded"
    with Db(migrated_engine) as db:
        row = db.get(SessionRow, uuid.UUID(created["session_id"]))
        assert row is not None and row.client_info is None


def test_complete_status_and_auth_rules(client: TestClient, study_id: int) -> None:
    c = create_session(client)  # not consented
    assert complete(client, c, {}).status_code == 409
    consent(client, c)
    assert complete(client, c, {}).status_code == 200  # nothing recorded, nothing missing
    r = client.post(f"/api/sessions/{c['session_id']}/complete", json={"last_seq": {}})
    assert r.status_code == 401
    bad: list[dict[str, Any]] = [
        {"last_seq": {"video": 1}},
        {"last_seq": {"webcam": -1}},
        {},
    ]
    for body in bad:
        r = client.post(f"/api/sessions/{c['session_id']}/complete", json=body, headers=auth(c))
        assert r.status_code == 422, body


def test_complete_refuses_an_absurd_last_seq(make_client: MakeClient, study_id: int) -> None:
    client = make_client(max_chunks_per_stream=5)
    c = create_session(client)
    consent(client, c)
    assert put(client, c, "events", 0, events_chunk(ev(1.0, "session_start"))).status_code == 200
    r = complete(client, c, {"events": 5})  # 6th chunk would be seq 5: over the limit
    assert r.status_code == 422 and "exceeds 5" in r.json()["detail"]
    r = complete(client, c, {"events": 4})  # within the limit: reports the 4 missing chunks
    assert r.status_code == 200 and r.json()["missing"] == {"events": [1, 2, 3, 4]}


# --- webcam, features and stimulus frames (milestone 2) ----------------------------------------


def flags_of(engine: Engine, created: dict[str, Any]) -> list[str]:
    with Db(engine) as db:
        row = db.get(SessionRow, uuid.UUID(created["session_id"]))
        assert row is not None
        return list(row.flags)


def test_complete_assembles_features_and_stimulus_frames_files(
    client: TestClient, created: dict[str, Any], data_root: Path, study_id: int
) -> None:
    chunks = upload_all(client, created)
    assert complete(client, created, LAST).json() == {"status": "uploaded", "missing": {}}
    raw = session_dir(data_root, study_id, created) / "raw"
    features = (raw / "browser_features.jsonl").read_bytes()
    frames = (raw / "stimulus_frames.jsonl").read_bytes()
    assert features == b"".join(gzip.decompress(c) for c in chunks["features"])
    assert frames == b"".join(gzip.decompress(c) for c in chunks["stimulus_frames"])
    assert [json.loads(line)["face"] for line in features.splitlines()] == [True, False, True]
    assert len(frames.splitlines()) == 3
    assert not (raw / "webcam.webm").exists()  # the remux belongs to the M3 ingest
    assert list(raw.rglob("*.tmp")) == []


def test_a_face_frame_with_an_unknown_brightness_is_accepted_at_complete(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    unmeasured = EXAMPLES["feature_record_unmeasured_brightness"]
    put(client, created, "features", 0, events_chunk(FEATURE, unmeasured))
    assert complete(client, created, {"features": 0}).json()["status"] == "uploaded"
    raw = session_dir(data_root, study_id, created) / "raw"
    lines = (raw / "browser_features.jsonl").read_text().splitlines()
    assert [json.loads(line)["brightness"] for line in lines] == [FEATURE["brightness"], None]
    assert flags_of(migrated_engine, created) == []


def test_session_without_features_or_frames_writes_no_such_files(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    put(client, created, "events", 0, events_chunk(ev(0.0, "session_start")))
    assert complete(client, created, {"events": 0}).json()["status"] == "uploaded"
    raw = session_dir(data_root, study_id, created) / "raw"
    assert not (raw / "browser_features.jsonl").exists()
    assert not (raw / "stimulus_frames.jsonl").exists()
    assert flags_of(migrated_engine, created) == []


def test_first_webcam_chunk_must_be_webm(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    for bad in (b"not a webm file", b"\x1a\x45\xdf", gzip.compress(WEBM)):
        r = put(client, created, "webcam", 0, bad)
        assert r.status_code == 422 and "WebM" in r.json()["detail"]
    assert chunk_count(migrated_engine, created) == 0
    assert not chunk_file(data_root, study_id, created, "webcam", 0).exists()
    # later chunks are plain continuation bytes, and chunk 0 may arrive after them
    assert put(client, created, "webcam", 1, b"continuation bytes").status_code == 200
    assert put(client, created, "webcam", 0, WEBM + b"header").status_code == 200


@pytest.mark.parametrize("stream", ["features", "stimulus_frames"])
def test_bad_gzip_in_features_and_frames_is_refused_at_the_put(
    client: TestClient, created: dict[str, Any], migrated_engine: Engine, stream: str
) -> None:
    good = events_chunk(FEATURE)
    corrupt_body = good[:12] + bytes([good[12] ^ 0xFF]) + good[13:]
    for bad in (b"this is not gzip", corrupt_body, good[:-6], good + b"junk", WEBM + b"webm"):
        r = put(client, created, stream, 0, bad)
        assert r.status_code == 422 and stream in r.json()["detail"], bad
    assert chunk_count(migrated_engine, created) == 0


@pytest.mark.parametrize("stream", ["features", "stimulus_frames"])
def test_gzip_bomb_in_features_and_frames_is_refused_at_the_put(
    make_client: MakeClient, study_id: int, migrated_engine: Engine, stream: str
) -> None:
    client = make_client(max_events_chunk_inflated_mb=1)
    c = create_session(client)
    consent(client, c)
    bomb = gzip.compress(b"0" * (20 * 1024 * 1024))
    assert len(bomb) < 100_000
    assert put(client, c, stream, 0, bomb).status_code == 422
    assert chunk_count(migrated_engine, c) == 0


def test_invalid_feature_line_finishes_the_session_and_flags_it(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    put(client, created, "events", 0, events_chunk(ev(0.0, "session_start")))
    wrong_size = {**FEATURE, "lm": FEATURE["lm"][:10]}
    put(client, created, "features", 0, events_chunk(FEATURE, wrong_size))
    put(client, created, "stimulus_frames", 0, events_chunk(FRAME))
    r = complete(client, created, {"events": 0, "features": 0, "stimulus_frames": 0})
    assert r.json()["status"] == "uploaded"
    raw = session_dir(data_root, study_id, created) / "raw"
    assert not (raw / "browser_features.jsonl").exists()  # nothing half-valid is written
    assert (raw / "stimulus_frames.jsonl").exists() and (raw / "events.jsonl").exists()
    assert flags_of(migrated_engine, created) == ["features_invalid"]


def test_invalid_stimulus_frame_line_flags_only_that_stream(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    put(client, created, "features", 0, events_chunk(FEATURE))
    put(client, created, "stimulus_frames", 0, events_chunk(FRAME, {"t": 1.0}))
    complete(client, created, {"features": 0, "stimulus_frames": 0})
    raw = session_dir(data_root, study_id, created) / "raw"
    assert (raw / "browser_features.jsonl").exists()
    assert not (raw / "stimulus_frames.jsonl").exists()
    assert flags_of(migrated_engine, created) == ["stimulus_frames_invalid"]


def gate(
    t: float,
    *,
    block: str = "before_video",
    attempt: int = 1,
    passed: bool | None = False,
    **change: Any,
) -> dict[str, Any]:
    data = {
        **EXAMPLES["event_data"]["calib_gate_result"],
        "block": block,
        "attempt": attempt,
        "passed": passed,
        "skipped": passed is None,
        **change,
    }
    return {"t": t, "type": "calib_gate_result", "data": data}


@pytest.mark.parametrize(
    ("results", "flagged"),
    [
        pytest.param([gate(1, passed=False)], True, id="one failed attempt"),
        pytest.param(
            [gate(1, passed=False), gate(2, attempt=2, passed=False)], True, id="all failed"
        ),
        pytest.param(
            [gate(1, passed=False), gate(2, attempt=2, passed=True)], False, id="retry passed"
        ),
        pytest.param([gate(1, passed=True)], False, id="passed"),
        pytest.param([gate(1, passed=None, reason="no_validation")], False, id="skipped"),
        pytest.param(
            [gate(1, passed=True), gate(2, block="between_videos", passed=False)],
            False,
            id="a later block never counts",
        ),
        pytest.param([], False, id="no gate events"),
    ],
)
def test_calibration_gate_failed_flag_comes_from_the_events(
    client: TestClient,
    created: dict[str, Any],
    migrated_engine: Engine,
    results: list[dict[str, Any]],
    flagged: bool,
) -> None:
    put(client, created, "events", 0, events_chunk(ev(0.0, "session_start"), *results))
    assert complete(client, created, {"events": 0}).json()["status"] == "uploaded"
    assert flags_of(migrated_engine, created) == (["calibration_gate_failed"] if flagged else [])


def test_gate_flag_is_not_added_twice(
    client: TestClient, created: dict[str, Any], migrated_engine: Engine
) -> None:
    put(client, created, "events", 0, events_chunk(gate(1.0, passed=False)))
    complete(client, created, {"events": 0})
    complete(client, created, {"events": 0})
    assert flags_of(migrated_engine, created) == ["calibration_gate_failed"]


def test_malformed_gate_result_flags_events_invalid(
    client: TestClient,
    created: dict[str, Any],
    data_root: Path,
    study_id: int,
    migrated_engine: Engine,
) -> None:
    broken = gate(1.0, passed=False, reason="because")
    put(client, created, "events", 0, events_chunk(ev(0.0, "session_start"), broken))
    assert complete(client, created, {"events": 0}).json()["status"] == "uploaded"
    assert not (session_dir(data_root, study_id, created) / "raw" / "events.jsonl").exists()
    assert flags_of(migrated_engine, created) == ["events_invalid"]
