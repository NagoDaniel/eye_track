"""What the admin diagnostics page shows (PLAN §12.3): disk use of the data folder, the job queue
and failed jobs, the worker's heartbeat, the app version and the browsers and cameras of recent
sessions. Light module: the api process reads it. Nothing here touches participant video."""

import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session as Db

from app.models import Job, JobStatus, WorkerHeartbeat
from app.models import Session as SessionRow
from app.storage import events_path
from app.version import git_hash

# Top-level folders of `DATA_ROOT` that are sized one by one (PLAN §7).
FOLDERS = ("studies", "analysis", "videos")
RECENT_SESSIONS = 20  # how many of the latest sessions the browser table lists
FAILED_JOBS_SHOWN = 10
LOG_EXCERPT_CHARS = 300
EVENT_LINES_SCANNED = 200  # `recorder_start` comes near the start of the events file


@dataclass(frozen=True)
class DiskReport:
    total: int
    used: int
    free: int
    folders: dict[str, int]  # bytes per top-level folder of DATA_ROOT (0 when it does not exist)


def folder_bytes(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())


def disk_report(root: Path) -> DiskReport:
    usage = shutil.disk_usage(root if root.exists() else root.parent)
    return DiskReport(
        usage.total, usage.used, usage.free, {n: folder_bytes(root / n) for n in FOLDERS}
    )


def queue_report(db: Db) -> dict[str, Any]:
    rows = db.execute(select(Job.status, func.count()).group_by(Job.status)).all()
    counts = {status: n for status, n in rows}
    oldest = db.scalar(select(func.min(Job.created_at)).where(Job.status == JobStatus.QUEUED))
    failed = db.scalars(
        select(Job)
        .where(Job.status == JobStatus.FAILED)
        .order_by(Job.id.desc())
        .limit(FAILED_JOBS_SHOWN)
    ).all()
    return {
        "queued": counts.get(JobStatus.QUEUED, 0),
        "running": counts.get(JobStatus.RUNNING, 0),
        "failed": counts.get(JobStatus.FAILED, 0),
        "done": counts.get(JobStatus.DONE, 0),
        "oldest_queued_age_s": None if oldest is None else _age_s(oldest),
        "last_failed": [
            {
                "id": j.id,
                "kind": j.kind.value,
                "finished_at": j.finished_at,
                "log": j.log.strip()[-LOG_EXCERPT_CHARS:],
            }
            for j in failed
        ],
    }


def _age_s(moment: datetime) -> float:
    return (datetime.now(UTC) - moment).total_seconds()


def worker_report(db: Db, max_age_s: float) -> dict[str, Any]:
    """The newest heartbeat. `alive` when it is not older than the setting that the container
    healthcheck uses too (`WORKER_HEARTBEAT_MAX_AGE_S`)."""
    beats = db.scalars(select(WorkerHeartbeat)).all()
    if not beats:
        return {"workers": 0, "age_s": None, "alive": False}
    newest = max(b.beat_at for b in beats)
    age = _age_s(newest)
    return {"workers": len(beats), "age_s": age, "alive": age <= max_age_s}


# (name, regex of the version); the first that matches wins, as Edge and Opera say "Chrome" too
_UA = (
    ("Edge", re.compile(r"Edg/(\d+)")),
    ("Opera", re.compile(r"OPR/(\d+)")),
    ("Firefox", re.compile(r"Firefox/(\d+)")),
    ("Chrome", re.compile(r"Chrome/(\d+)")),
    ("Safari", re.compile(r"Version/(\d+)[^ ]* (?:Mobile/\S+ )?Safari/")),
)


def browser_of(user_agent: str | None) -> str:
    """ "Chrome 130", "Edge 130", "Firefox 131", "Safari 17" or "other"."""
    for name, pattern in _UA:
        found = pattern.search(user_agent or "")
        if found:
            return f"{name} {found.group(1)}"
    return "other"


def camera_of(root: Path, study_id: int, session_id: Any) -> dict[str, Any] | None:
    """The camera settings the browser reported when it started recording (`recorder_start`)."""
    path = events_path(root, study_id, session_id)
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as lines:
        for _, line in zip(range(EVENT_LINES_SCANNED), lines, strict=False):
            if '"recorder_start"' not in line:
                continue
            try:
                settings = json.loads(line)["data"]["settings"]
            except (ValueError, KeyError, TypeError):
                return None
            if not isinstance(settings, dict):
                return None
            return {k: settings.get(k) for k in ("width", "height", "frameRate")}
    return None


def browsers_report(db: Db, root: Path) -> dict[str, Any]:
    rows = db.scalars(
        select(SessionRow).order_by(SessionRow.created_at.desc()).limit(RECENT_SESSIONS)
    )
    sessions: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for s in rows:
        info = s.client_info or {}
        browser = browser_of(info.get("user_agent"))
        counts[browser] = counts.get(browser, 0) + 1
        sessions.append(
            {
                "id": str(s.id),
                "created_at": s.created_at,
                "browser": browser,
                "platform": info.get("platform"),
                "cores": info.get("hardware_concurrency"),
                "screen": info.get("screen"),
                "camera": camera_of(root, s.study_id, s.id),
            }
        )
    return {"sessions": sessions, "counts": dict(sorted(counts.items(), key=lambda kv: -kv[1]))}


def diagnostics(db: Db, root: Path, heartbeat_max_age_s: float) -> dict[str, Any]:
    return {
        "version": git_hash(),
        "disk": disk_report(root),
        "queue": queue_report(db),
        "worker": worker_report(db, heartbeat_max_age_s),
        "browsers": browsers_report(db, root),
    }
