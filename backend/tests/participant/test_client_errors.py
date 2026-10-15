import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.logging import configure_logging
from tests.dbutil import make_settings
from tests.participant.helpers import MakeClient

REPORT = {
    "message": "TypeError: x is undefined",
    "stack": "at foo (app.js:1:2)\nat bar (app.js:3:4)",
    "url": "https://example.org/s/phys1",
    "t": 12.5,
    "session_id": "3f2b8c1e-5d4a-4e9b-9c1a-2f6d7e8a9b0c",
}


def error_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    out = capsys.readouterr().err.strip().splitlines()
    records = [json.loads(line) for line in out]  # every line must be valid JSON
    return [r for r in records if r["msg"] == "client error"]


def test_default_limit() -> None:
    assert make_settings().client_error_max_bytes == 16384


def test_accepted_with_and_without_session_id(client: TestClient) -> None:
    assert client.post("/api/client-errors", json=REPORT).status_code == 204
    r = client.post("/api/client-errors", json={"message": "boom"})
    assert r.status_code == 204 and r.content == b""
    r = client.post("/api/client-errors", json={**REPORT, "session_id": None})
    assert r.status_code == 204


def test_log_line_is_json_with_the_report_and_nothing_else(
    client: TestClient, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    headers = {
        "Authorization": "Bearer secret-token",
        "Cookie": "a=b",
        "X-Forwarded-For": "1.2.3.4",
    }
    assert client.post("/api/client-errors", json=REPORT, headers=headers).status_code == 204
    (rec,) = error_lines(capsys)
    assert rec["level"] == "WARNING"
    assert rec["error_message"] == REPORT["message"]
    assert rec["stack"] == REPORT["stack"] and rec["url"] == REPORT["url"]
    assert rec["t"] == 12.5 and rec["session_id"] == REPORT["session_id"]
    assert set(rec) == {
        "ts",
        "level",
        "logger",
        "msg",
        "error_message",
        "stack",
        "url",
        "t",
        "session_id",
    }


def test_oversize_body_is_413_and_not_logged(
    make_client: MakeClient, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO")
    client = make_client(client_error_max_bytes=200)
    small = json.dumps({"message": "x" * 50})
    assert client.post("/api/client-errors", content=small).status_code == 204
    big = json.dumps({"message": "x" * 300})
    assert client.post("/api/client-errors", content=big).status_code == 413
    assert len(error_lines(capsys)) == 1


def test_exactly_the_limit_is_accepted(make_client: MakeClient) -> None:
    body = json.dumps({"message": "x" * 100})
    client = make_client(client_error_max_bytes=len(body))
    assert client.post("/api/client-errors", content=body).status_code == 204
    assert client.post("/api/client-errors", content=body + " ").status_code == 413


@pytest.mark.parametrize(
    "body", [b"not json", b"{}", b'{"message": 3}', b'{"message": "m", "x": 1}']
)
def test_invalid_report_is_422(client: TestClient, body: bytes) -> None:
    assert client.post("/api/client-errors", content=body).status_code == 422


def test_rate_limit_applies_per_ip(make_client: MakeClient) -> None:
    client = make_client(rate_limit_per_hour=2)
    assert [client.post("/api/client-errors", json=REPORT).status_code for _ in range(2)] == [
        204
    ] * 2
    assert client.post("/api/client-errors", json=REPORT).status_code == 429
    other = {"X-Forwarded-For": "198.51.100.7"}
    assert client.post("/api/client-errors", json=REPORT, headers=other).status_code == 204
