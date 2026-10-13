"""Helpers shared by the participant API tests."""

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

STUDY_CODE = "phys1"
PARTICIPANT = "AB0315"

MakeClient = Callable[..., TestClient]


def create_session(client: TestClient, **headers: str) -> dict[str, Any]:
    r = client.post(
        "/api/sessions",
        json={"study_code": STUDY_CODE, "participant_code": PARTICIPANT},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body: dict[str, Any] = r.json()
    return body


def auth(created: dict[str, Any]) -> dict[str, str]:
    return {"Authorization": f"Bearer {created['upload_token']}"}


def consent(client: TestClient, created: dict[str, Any]) -> None:
    r = client.post(
        f"/api/sessions/{created['session_id']}/consent",
        json={"consent_version": "v1"},
        headers=auth(created),
    )
    assert r.status_code == 200, r.text
