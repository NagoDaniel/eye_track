"""Admin analysis: start runs of `analyze_video`, list them, show one, download its files and the
ZIP (PLAN §6, §9)."""

import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session as DbSession
from starlette.background import BackgroundTask

from app import analysis_view
from app.analysis_runs import queue_analysis, queue_comparison
from app.api.pages import Page, check_form_csrf, page_admin, render
from app.auth import csrf_protected, current_admin, get_db
from app.models import AdminUser
from app.service_errors import AdminError
from app.settings import Settings
from app.studies import list_studies
from app.videos import list_videos

log = logging.getLogger("app")
router = APIRouter(prefix="/admin")

MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".png": "image/png",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".md": "text/markdown; charset=utf-8",
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


# --- starting and listing runs ----------------------------------------------------------------


class StartRequest(BaseModel):
    """`POST /admin/api/analysis`."""

    model_config = ConfigDict(extra="forbid")
    study_id: int
    video_id: int
    gaze_columns: str = "drift_corrected"
    aoi_tolerance_k: float = 0.0
    heatmap_dots: bool = False
    pipeline: str = "a"


@router.post("/api/analysis", status_code=201, dependencies=[Depends(csrf_protected)])
def api_start(
    body: StartRequest,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> dict[str, Any]:
    job, run_id = queue_analysis(
        db,
        body.study_id,
        body.video_id,
        body.gaze_columns,
        body.aoi_tolerance_k,
        body.heatmap_dots,
        body.pipeline,
    )
    db.commit()
    return {"job_id": job.id, "run_id": run_id, "args": job.args}


@router.get("/api/analysis")
def api_list(
    request: Request,
    study_id: int | None = None,
    video_id: int | None = None,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return analysis_view.list_runs(db, _root(request), study_id, video_id)


@router.get("/api/analysis/{study_id}/{video_id}/{run_id}")
def api_run(
    request: Request,
    study_id: int,
    video_id: int,
    run_id: str,
    _admin: AdminUser = Depends(current_admin),
) -> dict[str, Any]:
    return analysis_view.read_run(_root(request), study_id, video_id, run_id)


def _index(
    request: Request,
    page: Page,
    db: DbSession,
    error: str | None = None,
    queued: bool = False,
    status_code: int = 200,
) -> Response:
    studies, videos = list_studies(db), list_videos(db)
    return render(
        request,
        page,
        "analysis.html",
        status_code,
        studies=studies,
        videos=videos,
        study_names={s.id: f"{s.name} ({s.code})" for s in studies},
        video_names={v.id: v.title for v in videos},
        runs=analysis_view.list_runs(db, _root(request))[:100],
        comparisons=analysis_view.list_comparisons(db, _root(request))[:50],
        error=error,
        queued=queued,
    )


@router.get("/analysis")
def index_page(
    request: Request,
    queued: bool = False,
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    return _index(request, page, db, queued=queued)


@router.post("/analysis")
def start_form(
    request: Request,
    study_id: Annotated[int, Form()],
    video_id: Annotated[int, Form()],
    gaze_columns: Annotated[str, Form()] = "drift_corrected",
    aoi_tolerance_k: Annotated[float, Form()] = 0.0,
    heatmap_dots: Annotated[bool, Form()] = False,
    pipeline: Annotated[str, Form()] = "a",
    csrf_token: Annotated[str, Form()] = "",
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    check_form_csrf(page, csrf_token)
    try:
        queue_analysis(
            db, study_id, video_id, gaze_columns, aoi_tolerance_k, heatmap_dots, pipeline
        )
    except AdminError as exc:
        db.rollback()
        return _index(request, page, db, error=str(exc), status_code=exc.status_code)
    db.commit()
    return RedirectResponse("/admin/analysis?queued=true", status_code=303)


@router.get(RUN)
def run_page(
    request: Request,
    study_id: int,
    video_id: int,
    run_id: str,
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    root = _root(request)
    record = analysis_view.read_run(root, study_id, video_id, run_id)
    run_dir = analysis_view.run_directory(root, study_id, video_id, run_id)
    outputs: list[str] = record["outputs"]
    study = next((s for s in list_studies(db) if s.id == study_id), None)
    video = next((v for v in list_videos(db) if v.id == video_id), None)
    return render(
        request,
        page,
        "analysis_run.html",
        record=record,
        study=study,
        video=video,
        base=f"/admin/analysis/{study_id}/{video_id}/{run_id}",
        static_pictures=[n for n in outputs if n.startswith("static/")],
        csv_files=[n for n in outputs if n.endswith(".csv")],
        aoi_rows=analysis_view.read_csv_rows(run_dir / "aoi_metrics_wide.csv", limit=200),
        has_heatmap="heatmap.mp4" in outputs,
        has_synchrony="synchrony.png" in outputs,
    )


# --- comparisons of Pipeline A and B (PLAN §9.5) ------------------------------------------------

COMPARE = "/comparisons/{study_id}/{run_id}"


class CompareRequest(BaseModel):
    """`POST /admin/api/comparisons`."""

    model_config = ConfigDict(extra="forbid")
    study_id: int


@router.post("/api/comparisons", status_code=201, dependencies=[Depends(csrf_protected)])
def api_compare(
    body: CompareRequest,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> dict[str, Any]:
    job, run_id = queue_comparison(db, body.study_id)
    db.commit()
    return {"job_id": job.id, "run_id": run_id, "args": job.args}


@router.get("/api/comparisons")
def api_comparisons(
    request: Request,
    study_id: int | None = None,
    _admin: AdminUser = Depends(current_admin),
    db: DbSession = Depends(get_db),
) -> list[dict[str, Any]]:
    return analysis_view.list_comparisons(db, _root(request), study_id)


@router.get("/api/comparisons/{study_id}/{run_id}")
def api_comparison(
    request: Request,
    study_id: int,
    run_id: str,
    _admin: AdminUser = Depends(current_admin),
) -> dict[str, Any]:
    return analysis_view.read_compare(_root(request), study_id, run_id)


@router.post("/comparisons")
def compare_form(
    request: Request,
    study_id: Annotated[int, Form()],
    csrf_token: Annotated[str, Form()] = "",
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    check_form_csrf(page, csrf_token)
    try:
        queue_comparison(db, study_id)
    except AdminError as exc:
        db.rollback()
        return _index(request, page, db, error=str(exc), status_code=exc.status_code)
    db.commit()
    return RedirectResponse("/admin/analysis?queued=true", status_code=303)


@router.get(COMPARE)
def comparison_page(
    request: Request,
    study_id: int,
    run_id: str,
    page: Page = Depends(page_admin),
    db: DbSession = Depends(get_db),
) -> Response:
    root = _root(request)
    record = analysis_view.read_compare(root, study_id, run_id)
    run_dir = analysis_view.compare_directory(root, study_id, run_id)
    study = next((s for s in list_studies(db) if s.id == study_id), None)
    return render(
        request,
        page,
        "comparison.html",
        record=record,
        study=study,
        base=f"/admin/comparisons/{study_id}/{run_id}",
        statistics=analysis_view.read_csv_rows(run_dir / "statistics.csv"),
        sessions=analysis_view.read_csv_rows(run_dir / "comparison.csv", limit=200),
        has_plot="boxplots.png" in record["outputs"],
    )


@router.get(COMPARE + "/files/{name:path}")
def comparison_file(
    request: Request,
    study_id: int,
    run_id: str,
    name: str,
    _admin: AdminUser = Depends(current_admin),
) -> Response:
    path = analysis_view.compare_file(_root(request), study_id, run_id, name)
    media_type = MEDIA_TYPES.get(path.suffix, "application/octet-stream")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


@router.get(COMPARE + "/download.zip")
def comparison_zip(
    request: Request,
    study_id: int,
    run_id: str,
    _admin: AdminUser = Depends(current_admin),
) -> Response:
    archive = analysis_view.build_compare_zip(_root(request), study_id, run_id)
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=f"comparison_{study_id}_{run_id}.zip",
        headers={"Cache-Control": "no-store"},
        background=BackgroundTask(archive.unlink, missing_ok=True),
    )
