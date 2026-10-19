"""Admin analysis: runs of `analyze_video`, their files and the ZIP export (PLAN §6, §9).

The pages and the form for starting a run are added next to these routes.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app import analysis_view
from app.auth import current_admin
from app.models import AdminUser
from app.settings import Settings

log = logging.getLogger("app")
router = APIRouter(prefix="/admin")

MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".npz": "application/octet-stream",
}
RUN = "/analysis/{study_id}/{video_id}/{run_id}"


def _root(request: Request) -> Path:
    settings: Settings = request.app.state.settings
    return settings.data_root


@router.get(RUN + "/files/{name:path}")
def run_file(
    request: Request,
    study_id: int,
    video_id: int,
    run_id: str,
    name: str,
    _admin: AdminUser = Depends(current_admin),
) -> Response:
    """One output of a run (only the names `run.json` lists), for a logged-in admin."""
    path = analysis_view.run_file(_root(request), study_id, video_id, run_id, name)
    media_type = MEDIA_TYPES.get(path.suffix, "application/octet-stream")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


@router.get(RUN + "/download.zip")
def download(
    request: Request,
    study_id: int,
    video_id: int,
    run_id: str,
    _admin: AdminUser = Depends(current_admin),
) -> Response:
    """`run.json` and all outputs of the run as one ZIP (PLAN §6)."""
    archive = analysis_view.build_zip(_root(request), study_id, video_id, run_id)
    log.info("analysis run downloaded", extra={"run_id": run_id})
    name = f"analysis_{study_id}_{video_id}_{run_id}.zip"
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=name,
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(archive.unlink, missing_ok=True),
    )
