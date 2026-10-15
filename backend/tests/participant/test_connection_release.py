"""A stalled upload must not use up the database pool (PLAN §6; DECISIONS 1b review R3).

Runs a real uvicorn server on a database pool of ONE connection and leaves a chunk upload
half-sent. Before the fix the stalled request held the only connection and every other request
(including /api/health) timed out waiting for it.
"""

import socket
import threading
import time
from collections.abc import Generator
from pathlib import Path

import httpx2
import pytest
import uvicorn
from sqlalchemy import Engine, create_engine

from app.main import create_app
from tests.dbutil import make_settings
from tests.participant.helpers import PARTICIPANT, STUDY_CODE

POOL_TIMEOUT_S = 3


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
def server(migrated_engine: Engine, data_root: Path, study_id: int) -> Generator[str]:
    one_connection = create_engine(
        migrated_engine.url, pool_size=1, max_overflow=0, pool_timeout=POOL_TIMEOUT_S
    )
    app = create_app(make_settings(data_root=data_root), engine=one_connection)
    port = free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    uv = uvicorn.Server(config)
    thread = threading.Thread(target=uv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if uv.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn did not start")
    yield f"http://127.0.0.1:{port}"
    uv.should_exit = True
    thread.join(timeout=10)


def test_stalled_chunk_uploads_do_not_hold_database_connections(server: str) -> None:
    api = httpx2.Client(base_url=server, timeout=15)
    created = api.post(
        "/api/sessions", json={"study_code": STUDY_CODE, "participant_code": PARTICIPANT}
    ).json()
    headers = {"Authorization": f"Bearer {created['upload_token']}"}
    sid = created["session_id"]
    assert (
        api.post(
            f"/api/sessions/{sid}/consent", json={"consent_version": "v1"}, headers=headers
        ).status_code
        == 200
    )

    # Three uploads that announce 1000 bytes, send 10, and then stall (more than the pool has).
    stalled: list[socket.socket] = []
    try:
        for seq in range(3):
            s = socket.create_connection(("127.0.0.1", int(server.rsplit(":", 1)[1])))
            head = (
                f"PUT /api/sessions/{sid}/chunks/events/{seq} HTTP/1.1\r\n"
                f"Host: x\r\nAuthorization: Bearer {created['upload_token']}\r\n"
                f"X-Chunk-SHA256: {'0' * 64}\r\nContent-Length: 1000\r\n\r\n"
            )
            s.sendall(head.encode() + b"0123456789")
            stalled.append(s)
        time.sleep(1)  # let the server reach the half-read bodies

        started = time.monotonic()
        health = api.get("/api/health")
        again = api.get(f"/api/studies/{STUDY_CODE}")
        elapsed = time.monotonic() - started
        assert health.status_code == 200, health.text
        assert again.status_code == 200
        assert elapsed < POOL_TIMEOUT_S, f"requests waited {elapsed:.1f}s for a connection"
    finally:
        for s in stalled:
            s.close()
