"""The record of one `analyze_video` run: `run.json` (PLAN §9).

It holds the parameters, the included sessions, the excluded ones with their reasons and the code's
git hash, and is written when the run starts (status `running`) and again when it ends, so a run
that died is visible. Outputs sit next to it in `analysis/{study_id}/{video_id}/{run_id}/`.
"""

import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.storage import analysis_run_dir, atomic_write_bytes
from worker.analysis.config import AnalysisConfig, git_hash
from worker.analysis.select import Selection

RUN_FILE = "run.json"
RUN_VERSION = 1
RUNNING, DONE, FAILED = "running", "done", "failed"


def new_run_id(now: datetime | None = None) -> str:
    """Sorts by time; the random tail keeps two runs in the same second apart."""
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(3)}"


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
