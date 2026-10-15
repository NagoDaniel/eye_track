"""Admin studies on real PostgreSQL (PLAN §4, §4.1, §6)."""

import json
import uuid
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session as DbSession

from app.models import Session, Study, StudyMode, Video
from app.schemas import StudyConfig
from tests.adminutil import Admin, anonymous_client, csrf_from, logged_in_admin

URL = "/admin/api/studies"


@pytest.fixture
def admin(migrated_engine: Engine, tmp_path: Path) -> Generator[Admin]:
    with logged_in_admin(migrated_engine, tmp_path) as a:
        yield a


@pytest.fixture
def anon(migrated_engine: Engine, tmp_path: Path) -> Generator[TestClient]:
    with anonymous_client(migrated_engine, tmp_path) as c:
        yield c


def body(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"name": "Physics", "code": "phys1", "mode": "remote", "config": {}}
    base.update(over)
    return base


def add_video(engine: Engine) -> int:
    with DbSession(engine) as db:
        video = Video(
            title="v", file_path="videos/1/video.mp4", duration=1, width=1, height=1, fps=25,
            sha256="0" * 64,
        )  # fmt: skip
        db.add(video)
        db.commit()
        return video.id


def add_session(engine: Engine, study_id: int) -> None:
    with DbSession(engine) as db:
        db.add(Session(id=uuid.uuid4(), study_id=study_id, participant_code="AB0315",
                       upload_token_hash="x"))  # fmt: skip
        db.commit()


def test_crud_round_trip(admin: Admin) -> None:
    c = admin.client
    r = c.post(URL, json=body(), headers=admin.headers)
    assert r.status_code == 201, r.text
    study = r.json()
    # The stored config is the full one with the plan's defaults filled in.
    assert study["config"] == StudyConfig().model_dump(mode="json")
    assert study["session_count"] == 0

    sid = study["id"]
    assert c.get(f"{URL}/{sid}").json() == study
    assert [s["id"] for s in c.get(URL).json()] == [sid]

    new_config = {"calibration": {"margin": 0.1}, "debug_allowed": True}
    r = c.put(
        f"{URL}/{sid}",
        json=body(name="Physics 2", code="phys2", mode="lab", config=new_config),
        headers=admin.headers,
    )
    assert r.status_code == 200, r.text
    got = c.get(f"{URL}/{sid}").json()
    assert (got["name"], got["code"], got["mode"]) == ("Physics 2", "phys2", "lab")
    assert got["config"]["calibration"] == {"points": 13, "dot_duration_ms": 1500, "margin": 0.1}
    assert got["config"]["debug_allowed"] is True

    assert c.delete(f"{URL}/{sid}", headers=admin.headers).status_code == 204
    assert c.get(f"{URL}/{sid}").status_code == 404
    assert c.get(URL).json() == []


def test_update_keeping_own_code_is_not_a_duplicate(admin: Admin) -> None:
    sid = admin.client.post(URL, json=body(), headers=admin.headers).json()["id"]
    r = admin.client.put(f"{URL}/{sid}", json=body(name="Renamed"), headers=admin.headers)
    assert r.status_code == 200


def test_anonymous_gets_401_everywhere(anon: TestClient) -> None:
    assert anon.get(URL).status_code == 401
    assert anon.get(f"{URL}/1").status_code == 401
    assert anon.post(URL, json=body()).status_code == 401
    assert anon.put(f"{URL}/1", json=body()).status_code == 401
    assert anon.delete(f"{URL}/1").status_code == 401


def test_state_changing_routes_need_csrf(admin: Admin) -> None:
    c = admin.client
    sid = c.post(URL, json=body(), headers=admin.headers).json()["id"]
    bad = {"X-CSRF-Token": "wrong"}
    assert c.post(URL, json=body(code="other"), headers=bad).status_code == 403
    assert c.post(URL, json=body(code="other")).status_code == 403
    assert c.put(f"{URL}/{sid}", json=body(), headers=bad).status_code == 403
    assert c.delete(f"{URL}/{sid}").status_code == 403
    assert c.get(f"{URL}/{sid}").status_code == 200  # nothing was deleted


def test_invalid_config_message_names_the_field(admin: Admin) -> None:
    r = admin.client.post(
        URL, json=body(config={"calibration": {"points": 0}, "bogus": 1}), headers=admin.headers
    )
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert "calibration.points" in detail and "bogus" in detail


def test_duplicate_code_is_409(admin: Admin) -> None:
    assert admin.client.post(URL, json=body(), headers=admin.headers).status_code == 201
    r = admin.client.post(URL, json=body(name="Other"), headers=admin.headers)
    assert r.status_code == 409
    other = admin.client.post(URL, json=body(code="phys9"), headers=admin.headers).json()["id"]
    r = admin.client.put(f"{URL}/{other}", json=body(), headers=admin.headers)
    assert r.status_code == 409


@pytest.mark.parametrize("code", ["ab", "a" * 33, "has space", "über1", "a/b", ""])
def test_bad_study_code_is_422(admin: Admin, code: str) -> None:
    r = admin.client.post(URL, json=body(code=code), headers=admin.headers)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("code", ["abc", "A_b-9", "x" * 32])
def test_good_study_codes(admin: Admin, code: str) -> None:
    assert admin.client.post(URL, json=body(code=code), headers=admin.headers).status_code == 201


def test_bad_mode_is_422(admin: Admin) -> None:
    r = admin.client.post(URL, json=body(mode="hybrid"), headers=admin.headers)
    assert r.status_code == 422 and "mode" in r.json()["detail"]


def test_playlist_with_unknown_video_is_422_and_names_the_id(
    admin: Admin, migrated_engine: Engine
) -> None:
    video_id = add_video(migrated_engine)
    ok = admin.client.post(
        URL, json=body(config={"playlist": [str(video_id)]}), headers=admin.headers
    )
    assert ok.status_code == 201
    for bad in ("9999", "abc"):
        r = admin.client.post(
            URL,
            json=body(code="other", config={"playlist": [str(video_id), bad]}),
            headers=admin.headers,
        )
        assert r.status_code == 422
        assert repr(bad) in r.json()["detail"]


def test_unknown_consent_version_is_422(admin: Admin) -> None:
    r = admin.client.post(URL, json=body(config={"consent_version": "v999"}), headers=admin.headers)
    assert r.status_code == 422
    assert "v999" in r.json()["detail"]


def test_delete_with_sessions_is_409(admin: Admin, migrated_engine: Engine) -> None:
    sid = admin.client.post(URL, json=body(), headers=admin.headers).json()["id"]
    add_session(migrated_engine, sid)
    assert admin.client.get(f"{URL}/{sid}").json()["session_count"] == 1
    r = admin.client.delete(f"{URL}/{sid}", headers=admin.headers)
    assert r.status_code == 409
    assert admin.client.get(f"{URL}/{sid}").status_code == 200


# --- pages -----------------------------------------------------------------------------------


def test_pages_redirect_to_login_when_anonymous(anon: TestClient) -> None:
    for path in ("/admin/studies", "/admin/studies/1"):
        r = anon.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_landing_page_links_to_the_sections(admin: Admin) -> None:
    html = admin.client.get("/admin/").text
    for href in ("/admin/studies", "/admin/videos", "/admin/sessions"):
        assert f'href="{href}"' in html


def test_form_routes_create_edit_delete(admin: Admin, migrated_engine: Engine) -> None:
    c = admin.client
    page = c.get("/admin/studies")
    assert page.status_code == 200
    assert "<script" not in page.text
    token = csrf_from(page.text)
    default_config = StudyConfig().model_dump(mode="json")
    assert json.dumps(default_config, indent=2).replace('"', "&#34;") in page.text

    form = {"name": "Form study", "code": "form1", "mode": "lab", "config": "{}"}
    assert c.post("/admin/studies", data={**form, "csrf_token": "bad"}).status_code == 403
    r = c.post("/admin/studies", data={**form, "csrf_token": token})
    assert r.status_code == 303
    detail_url = r.headers["location"]
    assert detail_url.startswith("/admin/studies/")
    sid = int(detail_url.rsplit("/", 1)[1])

    with DbSession(migrated_engine) as db:
        study = db.get(Study, sid)
        assert study is not None
        assert study.mode is StudyMode.LAB
        assert study.config == default_config

    detail = c.get(detail_url, headers={"X-Forwarded-Proto": "https"})  # as set by Caddy
    assert detail.status_code == 200
    assert "https://testserver/s/form1" in detail.text  # participant link
    assert "http://testserver/s/form1" in c.get(detail_url).text  # dev: plain http
    assert "Form study" in detail.text

    edit = {**form, "name": "Edited", "config": json.dumps({"debug_allowed": True})}
    r = c.post(detail_url, data={**edit, "csrf_token": token})
    assert r.status_code == 303 and r.headers["location"] == f"{detail_url}?saved=true"
    assert "Saved." in c.get(r.headers["location"]).text
    with DbSession(migrated_engine) as db:
        study = db.get(Study, sid)
        assert study is not None and study.name == "Edited" and study.config["debug_allowed"]

    assert c.post(f"{detail_url}/delete", data={"csrf_token": "bad"}).status_code == 403
    r = c.post(f"{detail_url}/delete", data={"csrf_token": token})
    assert r.status_code == 303 and r.headers["location"] == "/admin/studies"
    assert c.get(detail_url).status_code == 404


def test_form_errors_are_shown_and_input_is_kept(admin: Admin) -> None:
    c = admin.client
    token = csrf_from(c.get("/admin/studies").text)
    form = {"name": "N", "code": "ab", "mode": "remote", "config": "{not json", "csrf_token": token}
    r = c.post("/admin/studies", data=form)
    assert r.status_code == 422
    assert "code must match" in r.text
    assert "{not json" in r.text  # what the user typed is still in the form
    form["code"] = "abc"
    r = c.post("/admin/studies", data=form)
    assert r.status_code == 422 and "config is not valid JSON" in r.text


def test_form_duplicate_code_and_delete_with_sessions(
    admin: Admin, migrated_engine: Engine
) -> None:
    c = admin.client
    token = csrf_from(c.get("/admin/studies").text)
    form = {"name": "N", "code": "dup1", "mode": "remote", "config": "{}", "csrf_token": token}
    r = c.post("/admin/studies", data=form)
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    assert c.post("/admin/studies", data=form).status_code == 409
    add_session(migrated_engine, sid)
    r = c.post(f"/admin/studies/{sid}/delete", data={"csrf_token": token})
    assert r.status_code == 409 and "session" in r.text
