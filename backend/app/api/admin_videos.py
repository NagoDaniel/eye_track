"""Admin videos: JSON API under `/admin/api/videos` and plain HTML pages (PLAN §4, §6, §7).

The upload path `/admin/api/videos` is the one the proxy lets through with the large body limit
(`MAX_VIDEO_UPLOAD_MB`, DECISIONS 1a T9), so uploads must stay on it.
"""

import json
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session as DbSession
from starlette.datastructures import UploadFile

from app import videos
from app.api.pages import Page, check_form_csrf, page_admin, render
from app.auth import csrf_matches, csrf_protected, current_admin, get_db, read_session
from app.models import AdminUser
from app.service_errors import AdminError, ConflictError, InvalidError, TooLargeError
from app.settings import Settings
from app.videos import MIB, MULTIPART_OVERHEAD_BYTES

router = APIRouter(prefix="/admin")
WARNING_PARAM = "warn"


class VideoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    aois: dict[str, Any] | None = None


def _data_root(request: Request) -> Path:
    settings: Settings = request.app.state.settings
    return settings.data_root


def _wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


# --- JSON API ------------------------------------------------------------------------------------


@router.post("/api/videos", status_code=201)
async def api_upload(
    request: Request,
    admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    """Multipart form: `file`, `title`, and the CSRF token as field `csrf_token` or header."""
    settings: Settings = request.app.state.settings
    max_bytes = settings.max_video_upload_mb * MIB
    # Read by hand, after the login check, so that a stranger's upload is not even parsed.
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > max_bytes + MULTIPART_OVERHEAD_BYTES:
        raise TooLargeError(f"upload is larger than {settings.max_video_upload_mb} MiB")
    form = await request.form()
    try:
        session = read_session(request)
        token = request.headers.get("x-csrf-token") or str(form.get("csrf_token", ""))
        if session is None or not csrf_matches(session, token):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        upload = form.get("file")
        try:
            if not isinstance(upload, UploadFile):
                raise InvalidError("file is required")
            video, warnings = await run_in_threadpool(
                videos.create_video,
                db,
                settings.data_root,
                str(form.get("title", "")),
                upload.file,
                max_bytes,
            )
        except AdminError as exc:
            if not _wants_html(request):
                raise
            page = Page(admin.username, session)
            return _list_page(request, page, db, str(exc), exc.status_code)
    finally:
        await form.close()
    if _wants_html(request):
        query = urlencode([(WARNING_PARAM, w) for w in warnings])
        return RedirectResponse(f"/admin/videos/{video.id}" + (f"?{query}" if query else ""), 303)
    out = videos.video_out(video, settings.data_root)
    return JSONResponse({**out, "warnings": warnings}, status_code=201)


@router.get("/api/videos")
def api_list(
    request: Request, _admin: AdminUser = Depends(current_admin), db: DbSession = Depends(get_db)
) -> list[dict[str, Any]]:
    root = _data_root(request)
    return [videos.video_out(v, root) for v in videos.list_videos(db)]


@router.get("/api/videos/{video_id}")
def api_get(
    request: Request,
    video_id: int,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> dict[str, Any]:
    return videos.video_out(videos.get_video(db, video_id), _data_root(request))


@router.put("/api/videos/{video_id}", dependencies=[Depends(csrf_protected)])
def api_update(
    request: Request, video_id: int, body: VideoIn, db: DbSession = Depends(get_db)
) -> dict[str, Any]:
    root = _data_root(request)
    return videos.video_out(videos.update_video(db, root, video_id, body.title, body.aois), root)


@router.delete("/api/videos/{video_id}", status_code=204, dependencies=[Depends(csrf_protected)])
def api_delete(request: Request, video_id: int, db: DbSession = Depends(get_db)) -> Response:
    videos.delete_video(db, _data_root(request), video_id)
    return Response(status_code=204)


# --- Pages ---------------------------------------------------------------------------------------


def _list_page(
    request: Request,
    page: Page,
    db: DbSession,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    root = _data_root(request)
    rows = [videos.video_out(v, root) for v in videos.list_videos(db)]
    settings: Settings = request.app.state.settings
    return render(
        request,
        page,
        "videos.html",
        status_code,
        videos=rows,
        error=error,
        max_upload_mb=settings.max_video_upload_mb,
    )


def _detail_page(
    request: Request,
    page: Page,
    db: DbSession,
    video_id: int,
    warnings: list[str] | None = None,
    aois_text: str | None = None,
    title: str | None = None,
    error: str | None = None,
    saved: bool = False,
    status_code: int = 200,
) -> Response:
    video = videos.video_out(videos.get_video(db, video_id), _data_root(request))
    if aois_text is None:
        aois_text = json.dumps(video["aois"], indent=2) if video["aois"] is not None else ""
    return render(
        request,
        page,
        "video.html",
        status_code,
        video=video,
        warnings=warnings or [],
        title=video["title"] if title is None else title,
        aois_text=aois_text,
        error=error,
        saved=saved,
    )


@router.get("/videos")
def list_page(
    request: Request, page: Page = Depends(page_admin), db: DbSession = Depends(get_db)
) -> Response:
    return _list_page(request, page, db)


@router.get("/videos/{video_id}")
def detail_page(
    request: Request,
    video_id: int,
    warn: Annotated[list[str] | None, Query()] = None,
    saved: bool = False,
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    return _detail_page(request, page, db, video_id, warnings=warn, saved=saved)


@router.post("/videos/{video_id}")
def update_form(
    request: Request,
    video_id: int,
    title: Annotated[str, Form()],
    aois: Annotated[str, Form()] = "",
    csrf_token: Annotated[str, Form()] = "",
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    check_form_csrf(page, csrf_token)
    try:
        videos.update_video(db, _data_root(request), video_id, title, videos.parse_aois(aois))
    except InvalidError as exc:
        return _detail_page(
            request,
            page,
            db,
            video_id,
            aois_text=aois,
            title=title,
            error=str(exc),
            status_code=exc.status_code,
        )
    return RedirectResponse(f"/admin/videos/{video_id}?saved=true", status_code=303)


@router.post("/videos/{video_id}/delete")
def delete_form(
    request: Request,
    video_id: int,
    csrf_token: Annotated[str, Form()] = "",
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    check_form_csrf(page, csrf_token)
    try:
        videos.delete_video(db, _data_root(request), video_id)
    except ConflictError as exc:
        return _detail_page(request, page, db, video_id, error=str(exc), status_code=409)
    return RedirectResponse("/admin/videos", status_code=303)
