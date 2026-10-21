"""The record of one `analyze_video` run: `run.json` (PLAN §9).

It holds the parameters, the included sessions, the excluded ones with their reasons and the code's
git hash, and is written when the run starts (status `running`) and again when it ends, so a run
that died is visible. Outputs sit next to it in `analysis/{study_id}/{video_id}/{run_id}/`.
"""

import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.analysis_runs import RUN_FILE
from app.storage import analysis_run_dir, atomic_write_bytes
from worker.analysis.config import AnalysisConfig, git_hash
from worker.analysis.select import Selection

RUN_VERSION = 1
RUNNING, DONE, FAILED = "running", "done", "failed"


@dataclass
class RunRecord:
    run_id: str
    study_id: int
    video_id: int
    config: AnalysisConfig
    started_at: str
    git_hash: str | None
    included: list[dict[str, Any]]
    excluded: list[dict[str, Any]]
    status: str = RUNNING
    error: str | None = None
    finished_at: str | None = None
    outputs: list[str] = field(default_factory=list)  # file names relative to the run directory
    results: dict[str, Any] = field(default_factory=dict)  # numbers the stages want to report

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": RUN_VERSION,
            "kind": "analyze_video",
            "run_id": self.run_id,
            "study_id": self.study_id,
            "video_id": self.video_id,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "git_hash": self.git_hash,
            "parameters": self.config.as_dict(),
            "included": self.included,
            "excluded": self.excluded,
            "outputs": self.outputs,
            "results": self.results,
        }

    def write(self, run_dir: Path) -> None:
        atomic_write_bytes(
            run_dir / RUN_FILE, (json.dumps(self.as_dict(), indent=2) + "\n").encode()
        )

    def finish(self, run_dir: Path, error: str | None = None) -> None:
        self.status = FAILED if error else DONE
        self.error = error
        self.finished_at = datetime.now(UTC).isoformat()
        self.write(run_dir)


def discard_unfinished_dir(run_dir: Path, run_id: str) -> bool:
    """Remove what an earlier attempt of the same run left behind. A requeued job (its worker died)
    has the same run id; its directory holds `run.json` with status `running`, or nothing yet.
    Raises FileExistsError when the run finished: an id is never reused for a finished run."""
    if not run_dir.exists():
        return False
    try:
        status = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))["status"]
    except (OSError, ValueError, KeyError):
        status = None  # the attempt died before it wrote the record
    if status in (DONE, FAILED):
        raise FileExistsError(f"analysis run {run_id} has already finished")
    shutil.rmtree(run_dir)
    return True


def discard_unfinished_run(root: Path, study_id: int, video_id: int, run_id: str) -> bool:
    return discard_unfinished_dir(analysis_run_dir(root, study_id, video_id, run_id), run_id)


def start_run(
    root: Path,
    study_id: int,
    video_id: int,
    run_id: str,
    config: AnalysisConfig,
    selection: Selection,
) -> tuple[RunRecord, Path]:
    """Create the run directory and write `run.json` with status `running`."""
    run_dir = analysis_run_dir(root, study_id, video_id, run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    record = RunRecord(
        run_id=run_id,
        study_id=study_id,
        video_id=video_id,
        config=config,
        started_at=datetime.now(UTC).isoformat(),
        git_hash=git_hash(),
        included=[
            {"session_id": str(i.id), "participant_code": i.participant_code}
            for i in selection.included
        ],
        excluded=[e.as_dict() for e in selection.excluded],
    )
    record.write(run_dir)
    return record, run_dir
