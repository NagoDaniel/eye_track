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
    # The playlist has its own table; the text box holds everything else.
    shown = {k: v for k, v in default_config.items() if k != "playlist"}
    assert json.dumps(shown, indent=2).replace('"', "&#34;") in page.text

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


# --- picking the videos and the debug toggle on the pages ---------------------------------------


def add_titled_video(engine: Engine, title: str) -> int:
    with DbSession(engine) as db:
        video = Video(title=title, file_path="x", duration=12.5, width=1280, height=720, fps=25,
                      sha256="0" * 64)  # fmt: skip
        db.add(video)
        db.commit()
        return video.id


def playlist_of(engine: Engine, study_id: int) -> list[str]:
    with DbSession(engine) as db:
        study = db.get(Study, study_id)
        assert study is not None
        return list(study.config["playlist"])


def form_with(token: str, videos: list[tuple[int, bool, str]], **over: str) -> dict[str, Any]:
    """The fields a browser sends: `(id, ticked, order typed)` per video row."""
    data: dict[str, Any] = {
        "name": "Picked", "code": "pick1", "mode": "remote", "config": "{}", "csrf_token": token,
        "video_ids": [str(v) for v, ticked, _ in videos if ticked],
        "all_video_ids": [str(v) for v, _, _ in videos],
        "positions": [order for _, _, order in videos],
    }  # fmt: skip
    data.update(over)
    return data


def test_new_study_page_lists_the_videos_to_pick(admin: Admin, migrated_engine: Engine) -> None:
    add_titled_video(migrated_engine, "Lecture one")
    add_titled_video(migrated_engine, "Lecture two")
    page = admin.client.get("/admin/studies").text
    assert "Lecture one" in page and "Lecture two" in page
    assert page.count('name="video_ids"') == 2
    assert "checked" not in page.split("<h2>New study</h2>")[1].split("<textarea")[0]


def test_new_study_can_be_created_by_ticking_videos_in_the_typed_order(
    admin: Admin, migrated_engine: Engine
) -> None:
    a = add_titled_video(migrated_engine, "A")
    b = add_titled_video(migrated_engine, "B")
    c = add_titled_video(migrated_engine, "C")
    token = csrf_from(admin.client.get("/admin/studies").text)
    rows = [(a, True, "3"), (b, False, ""), (c, True, "1")]
    r = admin.client.post("/admin/studies", data=form_with(token, rows))
    assert r.status_code == 303
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    assert playlist_of(migrated_engine, sid) == [str(c), str(a)]  # C (1) before A (3), B not shown


def test_unnumbered_ticked_videos_come_last_in_id_order(
    admin: Admin, migrated_engine: Engine
) -> None:
    a = add_titled_video(migrated_engine, "A")
    b = add_titled_video(migrated_engine, "B")
    c = add_titled_video(migrated_engine, "C")
    token = csrf_from(admin.client.get("/admin/studies").text)
    rows = [(a, True, ""), (b, True, "1"), (c, True, "")]
    r = admin.client.post("/admin/studies", data=form_with(token, rows))
    sid = int(r.headers["location"].rsplit("/", 1)[1])
    assert playlist_of(migrated_engine, sid) == [str(b), str(a), str(c)]


def test_study_page_shows_the_current_playlist_ticked_and_numbered(
    admin: Admin, migrated_engine: Engine
) -> None:
    a = add_titled_video(migrated_engine, "A")
    b = add_titled_video(migrated_engine, "B")
    created = admin.client.post(
        URL, json=body(config={"playlist": [str(b), str(a)]}), headers=admin.headers
    )
    page = admin.client.get(f"/admin/studies/{created.json()['id']}").text
    first = page.index(f'name="video_ids" value="{b}"')
    second = page.index(f'name="video_ids" value="{a}"')
    assert first < second  # the playlist order, not the id order
    settings = page.split("<h2>Settings</h2>")[1]  # the playlist table (the export boxes are above)
    assert settings.count(" checked") == 2
    assert 'name="positions" min="1" value="1"' in page
    assert '"playlist"' not in page.split("<textarea")[1].split("</textarea>")[0]


def test_editing_the_selection_changes_the_playlist_and_keeps_other_settings(
    admin: Admin, migrated_engine: Engine
) -> None:
    a = add_titled_video(migrated_engine, "A")
    b = add_titled_video(migrated_engine, "B")
    created = admin.client.post(
        URL,
        json=body(config={"playlist": [str(a)], "calibration": {"dot_duration_ms": 900}}),
        headers=admin.headers,
    )
    sid = created.json()["id"]
    token = csrf_from(admin.client.get(f"/admin/studies/{sid}").text)
    config = json.dumps({"calibration": {"dot_duration_ms": 900}})
    rows = [(a, True, "2"), (b, True, "1")]
    r = admin.client.post(
        f"/admin/studies/{sid}", data=form_with(token, rows, code="phys1", config=config)
    )
    assert r.status_code == 303
    assert playlist_of(migrated_engine, sid) == [str(b), str(a)]
    with DbSession(migrated_engine) as db:
        study = db.get(Study, sid)
        assert study is not None and study.config["calibration"]["dot_duration_ms"] == 900


def test_a_failed_save_keeps_the_ticked_videos(admin: Admin, migrated_engine: Engine) -> None:
    a = add_titled_video(migrated_engine, "A")
    b = add_titled_video(migrated_engine, "B")
    token = csrf_from(admin.client.get("/admin/studies").text)
    rows = [(a, False, ""), (b, True, "1")]
    r = admin.client.post("/admin/studies", data=form_with(token, rows, code="ab"))
    assert r.status_code == 422 and "code must match" in r.text
    assert r.text.count(" checked") == 1  # B is still ticked after the error


def test_config_that_is_not_json_is_still_reported_with_a_selection(
    admin: Admin, migrated_engine: Engine
) -> None:
    a = add_titled_video(migrated_engine, "A")
    token = csrf_from(admin.client.get("/admin/studies").text)
    r = admin.client.post(
        "/admin/studies", data=form_with(token, [(a, True, "1")], config="{not json")
    )
    assert r.status_code == 422 and "config is not valid JSON" in r.text


def test_the_json_api_still_takes_a_playlist_of_ids(admin: Admin, migrated_engine: Engine) -> None:
    a = add_titled_video(migrated_engine, "A")
    r = admin.client.post(URL, json=body(config={"playlist": [str(a)]}), headers=admin.headers)
    assert r.status_code == 201 and r.json()["config"]["playlist"] == [str(a)]


def test_debug_toggle_sets_debug_allowed_and_shows_the_debug_link(
    admin: Admin, migrated_engine: Engine
) -> None:
    created = admin.client.post(URL, json=body(), headers=admin.headers)
    sid = created.json()["id"]
    page = admin.client.get(f"/admin/studies/{sid}", headers={"X-Forwarded-Proto": "https"}).text
    assert "Debug mode is off" in page and "/s/phys1?debug=1" not in page
    token = csrf_from(page)

    r = admin.client.post(
        f"/admin/studies/{sid}/debug", data={"enabled": "true", "csrf_token": token}
    )
    assert r.status_code == 303
    with DbSession(migrated_engine) as db:
        study = db.get(Study, sid)
        assert study is not None and study.config["debug_allowed"] is True
    on = admin.client.get(r.headers["location"], headers={"X-Forwarded-Proto": "https"}).text
    assert "https://testserver/s/phys1?debug=1" in on
    assert "Turn debug mode off" in on and "Saved." in on

    r = admin.client.post(
        f"/admin/studies/{sid}/debug", data={"enabled": "false", "csrf_token": token}
    )
    assert r.status_code == 303
    with DbSession(migrated_engine) as db:
        study = db.get(Study, sid)
        assert study is not None and study.config["debug_allowed"] is False
    assert "Debug mode is off" in admin.client.get(f"/admin/studies/{sid}").text


def test_debug_toggle_keeps_the_rest_of_the_config(admin: Admin, migrated_engine: Engine) -> None:
    a = add_titled_video(migrated_engine, "A")
    created = admin.client.post(
        URL,
        json=body(config={"playlist": [str(a)], "calibration": {"margin": 0.1}}),
        headers=admin.headers,
    )
    sid = created.json()["id"]
    token = csrf_from(admin.client.get(f"/admin/studies/{sid}").text)
    admin.client.post(f"/admin/studies/{sid}/debug", data={"enabled": "true", "csrf_token": token})
    with DbSession(migrated_engine) as db:
        study = db.get(Study, sid)
        assert study is not None
        assert study.config["playlist"] == [str(a)] and study.config["calibration"]["margin"] == 0.1


def test_debug_toggle_needs_the_csrf_token(admin: Admin) -> None:
    sid = admin.client.post(URL, json=body(), headers=admin.headers).json()["id"]
    r = admin.client.post(
        f"/admin/studies/{sid}/debug", data={"enabled": "true", "csrf_token": "bad"}
    )
    assert r.status_code == 403


def test_debug_toggle_redirects_to_the_login_when_anonymous(anon: TestClient) -> None:
    r = anon.post("/admin/studies/1/debug", data={"enabled": "true", "csrf_token": "x"})
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"
