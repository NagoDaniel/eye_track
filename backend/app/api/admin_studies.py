"""Admin studies: JSON API under `/admin/api/studies` and plain HTML pages (PLAN §4, §6).

The routes are thin; the logic is in `app.studies`.
"""

import json
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session as DbSession

from app import analysis_view, jobs_view, studies
from app import videos as video_service
from app.api.pages import Page, check_form_csrf, page_admin, render
from app.auth import csrf_protected, current_admin, get_db
from app.models import AdminUser
from app.pipeline_b import queue_study
from app.schemas import StudyConfig
from app.service_errors import ConflictError, InvalidError
from app.settings import Settings

router = APIRouter(prefix="/admin")


class StudyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    code: str
    mode: str
    config: dict[str, Any]


# --- Playlist selection (the forms pick videos from a list; the JSON API takes ids as before) ----


def _video_rows(db: DbSession, playlist: list[str]) -> list[dict[str, Any]]:
    """Every video as a row for the selection table: the playlist first, in its order."""
    order = {video_id: i + 1 for i, video_id in enumerate(playlist)}
    rows = [
        {
            "id": str(v.id),
            "title": v.title,
            "duration": v.duration,
            "fps": v.fps,
            "checked": str(v.id) in order,
            "position": order.get(str(v.id), ""),
        }
        for v in video_service.list_videos(db)
    ]
    rows.sort(key=lambda r: (r["position"] == "", r["position"] or 0, int(r["id"])))
    return rows


def _playlist_from_form(selected: list[str], all_ids: list[str], positions: list[str]) -> list[str]:
    """The ticked videos ordered by the position typed next to each (unnumbered ones last)."""
    typed = dict(zip(all_ids, positions, strict=False))

    def key(video_id: str) -> tuple[int, int]:
        position = typed.get(video_id, "").strip()
        number = int(position) if position.isascii() and position.isdigit() else 10**9
        return number, int(video_id) if video_id.isascii() and video_id.isdigit() else 10**9

    return sorted(dict.fromkeys(selected), key=key)


def _config_without_playlist(config: dict[str, Any]) -> str:
    """The config text of a form: everything except the playlist, which has its own table."""
    return json.dumps({k: v for k, v in config.items() if k != "playlist"}, indent=2)


def _with_playlist(config_text: str, playlist: list[str]) -> object:
    """The posted config with the selected playlist; text that is not a JSON object is passed on
    unchanged so that the usual validation reports the problem."""
    try:
        decoded = json.loads(config_text)
    except ValueError:
        return config_text
    if isinstance(decoded, dict):
        return {**decoded, "playlist": playlist}
    return decoded


def _out(db: DbSession, study_id: int) -> dict[str, Any]:
    study = studies.get_study(db, study_id)
    return studies.study_out(study, studies.session_counts(db).get(study.id, 0))


# --- JSON API ------------------------------------------------------------------------------------


@router.get("/api/studies")
def api_list(
    _admin: AdminUser = Depends(current_admin), db: DbSession = Depends(get_db)
) -> list[dict[str, Any]]:
    counts = studies.session_counts(db)
    return [studies.study_out(s, counts.get(s.id, 0)) for s in studies.list_studies(db)]


@router.post("/api/studies", status_code=201, dependencies=[Depends(csrf_protected)])
def api_create(body: StudyIn, db: DbSession = Depends(get_db)) -> dict[str, Any]:
    study = studies.create_study(db, body.name, body.code, body.mode, body.config)
    db.commit()
    return _out(db, study.id)


@router.get("/api/studies/{study_id}")
def api_get(
    study_id: int, _admin: AdminUser = Depends(current_admin), db: DbSession = Depends(get_db)
) -> dict[str, Any]:
    return _out(db, study_id)


@router.put("/api/studies/{study_id}", dependencies=[Depends(csrf_protected)])
def api_update(study_id: int, body: StudyIn, db: DbSession = Depends(get_db)) -> dict[str, Any]:
    studies.update_study(db, study_id, body.name, body.code, body.mode, body.config)
    db.commit()
    return _out(db, study_id)


@router.delete("/api/studies/{study_id}", status_code=204, dependencies=[Depends(csrf_protected)])
def api_delete(study_id: int, db: DbSession = Depends(get_db)) -> Response:
    studies.delete_study(db, study_id)
    db.commit()
    return Response(status_code=204)


# --- Pages ---------------------------------------------------------------------------------------


def _list_page(
    request: Request,
    page: Page,
    db: DbSession,
    form: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
    playlist: list[str] | None = None,
) -> Response:
    counts = studies.session_counts(db)
    open_jobs = jobs_view.open_counts(db)
    rows = [
        {**studies.study_out(s, counts.get(s.id, 0)), "jobs": open_jobs.get(s.id)}
        for s in studies.list_studies(db)
    ]
    form = form or {
        "name": "",
        "code": "",
        "mode": "remote",
        "config": _config_without_playlist(StudyConfig().model_dump(mode="json")),
    }
    return render(
        request,
        page,
        "studies.html",
        status_code,
        studies=rows,
        form=form,
        error=error,
        videos=_video_rows(db, playlist or []),
    )


def _detail_page(
    request: Request,
    page: Page,
    db: DbSession,
    study_id: int,
    form: dict[str, str] | None = None,
    error: str | None = None,
    saved: bool = False,
    status_code: int = 200,
    playlist: list[str] | None = None,
    b_queued: int | None = None,
    b_left: int | None = None,
) -> Response:
    study = _out(db, study_id)
    data_root = request.app.state.settings.data_root
    form = form or {
        "name": study["name"],
        "code": study["code"],
        "mode": study["mode"],
        "config": _config_without_playlist(study["config"]),
    }
    if playlist is None:
        playlist = list(study["config"]["playlist"])
    # The api sees plain http from the proxy; Caddy says which scheme the browser used.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    link = f"{scheme}://{request.url.netloc}/s/{study['code']}"
    return render(
        request,
        page,
        "study.html",
        status_code,
        study=study,
        form=form,
        error=error,
        saved=saved,
        participant_link=link,
        jobs=jobs_view.jobs_of_study(db, study_id),
        runs=analysis_view.list_runs(db, data_root, study_id),
        comparisons=analysis_view.list_comparisons(db, data_root, study_id),
        video_names={v.id: v.title for v in video_service.list_videos(db)},
        # PLAN §12.1: `?debug=1` works for studies with `debug_allowed` (or an admin on the browser)
        debug_link=f"{link}?debug=1",
        videos=_video_rows(db, playlist),
        b_queued=b_queued,
        b_left=b_left,
    )


@router.get("/studies")
def list_page(
    request: Request, page: Page = Depends(page_admin), db: DbSession = Depends(get_db)
) -> Response:
    return _list_page(request, page, db)


@router.post("/studies")
def create_form(
    request: Request,
    name: Annotated[str, Form()],
    code: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    config: Annotated[str, Form()],
    video_ids: Annotated[list[str] | None, Form()] = None,
    all_video_ids: Annotated[list[str] | None, Form()] = None,
    positions: Annotated[list[str] | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    check_form_csrf(page, csrf_token)
    playlist = _playlist_from_form(video_ids or [], all_video_ids or [], positions or [])
    try:
        study = studies.create_study(db, name, code, mode, _with_playlist(config, playlist))
        db.commit()
    except (InvalidError, ConflictError) as exc:
        form = {"name": name, "code": code, "mode": mode, "config": config}
        return _list_page(request, page, db, form, str(exc), exc.status_code, playlist)
    return RedirectResponse(f"/admin/studies/{study.id}", status_code=303)


@router.get("/studies/{study_id}")
def detail_page(
    request: Request,
    study_id: int,
    saved: bool = False,
    b_queued: int | None = None,
    b_left: int | None = None,
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    return _detail_page(request, page, db, study_id, saved=saved, b_queued=b_queued, b_left=b_left)


@router.post("/studies/{study_id}")
def update_form(
    request: Request,
    study_id: int,
    name: Annotated[str, Form()],
    code: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    config: Annotated[str, Form()],
    video_ids: Annotated[list[str] | None, Form()] = None,
    all_video_ids: Annotated[list[str] | None, Form()] = None,
    positions: Annotated[list[str] | None, Form()] = None,
    csrf_token: Annotated[str, Form()] = "",
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    check_form_csrf(page, csrf_token)
    playlist = _playlist_from_form(video_ids or [], all_video_ids or [], positions or [])
    try:
        studies.update_study(db, study_id, name, code, mode, _with_playlist(config, playlist))
        db.commit()
    except (InvalidError, ConflictError) as exc:
        form = {"name": name, "code": code, "mode": mode, "config": config}
        return _detail_page(
            request,
            page,
            db,
            study_id,
            form=form,
            error=str(exc),
            status_code=exc.status_code,
            playlist=playlist,
        )
    return RedirectResponse(f"/admin/studies/{study_id}?saved=true", status_code=303)


@router.post("/studies/{study_id}/debug")
def debug_form(
    study_id: int,
    enabled: Annotated[str, Form()],
    csrf_token: Annotated[str, Form()] = "",
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    """Turn debug mode on or off for a study (`debug_allowed`, PLAN §12.1)."""
    check_form_csrf(page, csrf_token)
    study = studies.get_study(db, study_id)
    config = {**study.config, "debug_allowed": enabled == "true"}
    studies.update_study(db, study_id, study.name, study.code, study.mode.value, config)
    db.commit()
    return RedirectResponse(f"/admin/studies/{study_id}?saved=true", status_code=303)


@router.post("/studies/{study_id}/delete")
def delete_form(
    request: Request,
    study_id: int,
    csrf_token: Annotated[str, Form()] = "",
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    check_form_csrf(page, csrf_token)
    try:
        studies.delete_study(db, study_id)
        db.commit()
    except ConflictError as exc:
        return _detail_page(
            request, page, db, study_id, error=str(exc), status_code=exc.status_code
        )
    return RedirectResponse("/admin/studies", status_code=303)


def _run_b(request: Request, db: DbSession, study_id: int) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    studies.get_study(db, study_id)
    queued, left = queue_study(
        db, study_id, settings.data_root, (settings.models_root, settings.pipeline_b_model)
    )
    db.commit()
    return {
        "queued": [j.args["session_id"] for j in queued],
        "left_out": [{"session_id": str(s.id), "reason": reason} for s, reason in left],
    }


@router.post(
    "/api/studies/{study_id}/run-b", status_code=201, dependencies=[Depends(csrf_protected)]
)
def api_run_b(
    request: Request,
    study_id: int,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> dict[str, Any]:
    """Queue Pipeline B for every processed session of the study that can take it."""
    return _run_b(request, db, study_id)


@router.post("/studies/{study_id}/run-b")
def run_b_form(
    request: Request,
    study_id: int,
    csrf_token: Annotated[str, Form()] = "",
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    check_form_csrf(page, csrf_token)
    result = _run_b(request, db, study_id)
    return RedirectResponse(
        f"/admin/studies/{study_id}?b_queued={len(result['queued'])}&b_left={len(result['left_out'])}",
        status_code=303,
    )
