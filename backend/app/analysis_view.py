"""Analysis runs for the admin: what is on disk and in the job queue (PLAN §6, §9).

A run lives in `analysis/{study_id}/{video_id}/{run_id}/` (`run.json` plus its outputs). Runs
started with the CLI have no job row; a queued run has a job row and no directory yet; both are
listed.
"""

import csv
import json
import os
import re
import tempfile
import zipfile
from itertools import islice
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session as Db

from app.analysis_runs import RUN_FILE, analysis_jobs
from app.models import Job, JobStatus
from app.service_errors import NotFoundError
from app.storage import analysis_run_dir

RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
# Already compressed: storing them is faster and not bigger.
STORED = (".mp4", ".png", ".npz")


def run_directory(root: Path, study_id: int, video_id: int, run_id: str) -> Path:
    """The directory of a run; NotFoundError when the id is malformed or the run does not exist."""
    if not RUN_ID.fullmatch(run_id):
        raise NotFoundError(f"no analysis run called {run_id}")
    run_dir = analysis_run_dir(root, study_id, video_id, run_id)
    if not (run_dir / RUN_FILE).is_file():
        raise NotFoundError(f"analysis run {run_id} has not started or does not exist")
    return run_dir


def read_run(root: Path, study_id: int, video_id: int, run_id: str) -> dict[str, Any]:
    run_dir = run_directory(root, study_id, video_id, run_id)
    record: dict[str, Any] = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
    return record


def run_file(root: Path, study_id: int, video_id: int, run_id: str, name: str) -> Path:
    """An output of the run (or `run.json`) by the name `run.json` gives it; nothing else is
    served, so a name cannot lead out of the run directory."""
    record = read_run(root, study_id, video_id, run_id)
    if name != RUN_FILE and name not in record["outputs"]:
        raise NotFoundError(f"the run has no file called {name}")
    path = run_directory(root, study_id, video_id, run_id) / name
    if not path.is_file():
        raise NotFoundError(f"{name} is missing from the run directory")
    return path


def build_zip(root: Path, study_id: int, video_id: int, run_id: str) -> Path:
    """`run.json` and every output of the run in a ZIP, in a temporary file the caller deletes."""
    record = read_run(root, study_id, video_id, run_id)
    run_dir = run_directory(root, study_id, video_id, run_id)
    names = [RUN_FILE, *record["outputs"]]
    missing = [n for n in names if not (run_dir / n).is_file()]
    if missing:
        raise NotFoundError(f"files of the run are missing: {', '.join(missing)}")
    descriptor, name_of_zip = tempfile.mkstemp(prefix="analysis-", suffix=".zip")
    os.close(descriptor)
    try:
        with zipfile.ZipFile(name_of_zip, "w") as archive:
            for name in names:
                method = zipfile.ZIP_STORED if name.endswith(STORED) else zipfile.ZIP_DEFLATED
                archive.write(run_dir / name, f"{run_id}/{name}", compress_type=method)
    except BaseException:
        Path(name_of_zip).unlink(missing_ok=True)
        raise
    return Path(name_of_zip)


def _from_record(record: dict[str, Any], job: Job | None) -> dict[str, Any]:
    status, error = record["status"], record["error"]
    if status == "running" and job is not None and job.status is JobStatus.FAILED:
        # the worker died or gave up: the record never got its end
        status, error = "failed", job.log
    return {
        "study_id": record["study_id"],
        "video_id": record["video_id"],
        "run_id": record["run_id"],
        "status": status,
        "error": error,
        "started_at": record["started_at"],
        "git_hash": record["git_hash"],
        "pipeline": record["parameters"].get("pipeline", "a"),
        "gaze_columns": record["parameters"]["gaze_columns"],
        "aoi_tolerance_k": record["parameters"]["aoi_tolerance_k"],
        "included": len(record["included"]),
        "excluded": len(record["excluded"]),
        "has_record": True,
        "job_id": None if job is None else job.id,
    }


def _from_job(job: Job) -> dict[str, Any]:
    args = job.args
    status = {JobStatus.QUEUED: "queued", JobStatus.RUNNING: "running"}.get(job.status)
    return {
        "study_id": args["study_id"],
        "video_id": args["video_id"],
        "run_id": args["run_id"],
        "status": status or "failed",
        "error": None if status else job.log,
        "started_at": None if job.started_at is None else job.started_at.isoformat(),
        "git_hash": None,
        "pipeline": args.get("pipeline", "a"),
        "gaze_columns": args.get("gaze_columns", "drift_corrected"),
        "aoi_tolerance_k": args.get("aoi_tolerance_k", 0.0),
        "included": 0,
        "excluded": 0,
        "has_record": False,
        "job_id": job.id,
    }


def list_runs(
    db: Db, root: Path, study_id: int | None = None, video_id: int | None = None
) -> list[dict[str, Any]]:
    """Every run, newest first (the run id starts with its start time)."""
    jobs = {str(j.args.get("run_id")): j for j in analysis_jobs(db, study_id, video_id)}
    found: dict[str, dict[str, Any]] = {}
    pattern = (
        f"{study_id if study_id is not None else '*'}/{video_id if video_id is not None else '*'}"
    )
    for path in (root / "analysis").glob(f"{pattern}/*/{RUN_FILE}"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            entry = _from_record(record, jobs.get(record["run_id"]))
        except (OSError, ValueError, KeyError):
            continue  # a half-written or foreign file; the run is not shown
        found[record["run_id"]] = entry
    for run_id, job in jobs.items():
        if run_id not in found:
            found[run_id] = _from_job(job)
    return sorted(found.values(), key=lambda r: r["run_id"], reverse=True)


def read_csv_rows(path: Path, limit: int | None = None) -> list[dict[str, str]]:
    """The rows of an output CSV as dicts (at most `limit`); an empty list when there is no file."""
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return list(islice(reader, limit))


def participant_analysis(
    db: Db, root: Path, study_id: int, participant_code: str
) -> list[dict[str, Any]]:
    """For each video, the latest finished run of the study that included the participant: their
    AOI metrics and mean NSS (DECISIONS 2026-10-14 and 2026-10-19)."""
    found: list[dict[str, Any]] = []
    seen: set[int] = set()
    for run in list_runs(db, root, study_id):
        if run["status"] != "done" or run["video_id"] in seen or not run["has_record"]:
            continue
        record = read_run(root, run["study_id"], run["video_id"], run["run_id"])
        if participant_code not in {i["participant_code"] for i in record["included"]}:
            continue
        seen.add(run["video_id"])
        run_dir = run_directory(root, run["study_id"], run["video_id"], run["run_id"])
        wide = read_csv_rows(run_dir / "aoi_metrics_wide.csv")
        nss = (record["results"].get("synchrony") or {}).get("nss_by_participant") or {}
        found.append({
            "video_id": run["video_id"],
            "run_id": run["run_id"],
            "started_at": record["started_at"],
            "aoi_rows": [r for r in wide if r["participant_code"] == participant_code],
            "nss_mean": nss.get(participant_code),
        })  # fmt: skip
    return found
