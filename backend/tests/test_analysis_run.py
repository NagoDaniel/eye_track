"""`run.json` and the run directory (PLAN §9)."""

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.storage import analysis_run_dir
from tests.test_analysis_select import info
from worker.analysis.config import AnalysisConfig
from worker.analysis.run import DONE, FAILED, RUN_FILE, RUNNING, new_run_id, start_run
from worker.analysis.select import Excluded, Selection


def selection() -> Selection:
    return Selection(
        included=[info(1, "A")],
        excluded=[Excluded(uuid.UUID(int=2), "B", ["video_not_watched"])],
    )


def test_run_ids_sort_by_time_and_differ() -> None:
    a = new_run_id(datetime(2026, 10, 19, 12, 0, 1, tzinfo=UTC))
    b = new_run_id(datetime(2026, 10, 19, 12, 0, 2, tzinfo=UTC))
    assert re.fullmatch(r"20261019T120001Z-[0-9a-f]{6}", a) and a < b
    now = datetime(2026, 10, 19, 12, 0, 1, tzinfo=UTC)
    assert new_run_id(now) != new_run_id(now)


def test_start_writes_run_json_with_everything_the_plan_asks_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_SHA", "abc123")
    config = AnalysisConfig(gaze_columns="drift_corrected", aoi_tolerance_k=1.5)
    record, run_dir = start_run(tmp_path, 3, 7, "run-1", config, selection())

    assert run_dir == analysis_run_dir(tmp_path, 3, 7, "run-1")
    assert run_dir == tmp_path / "analysis" / "3" / "7" / "run-1"
    data = json.loads((run_dir / RUN_FILE).read_text())
    assert data["status"] == RUNNING and data["finished_at"] is None
    assert (data["run_id"], data["study_id"], data["video_id"]) == ("run-1", 3, 7)
    assert data["git_hash"] == "abc123"
    assert data["parameters"]["gaze_columns"] == "drift_corrected"
    assert data["parameters"]["aoi_tolerance_k"] == 1.5
    assert data["parameters"]["pipeline"] == "a"
    assert data["included"] == [{"session_id": str(uuid.UUID(int=1)), "participant_code": "A"}]
    assert data["excluded"] == [
        {
            "session_id": str(uuid.UUID(int=2)),
            "participant_code": "B",
            "reasons": ["video_not_watched"],
        }  # fmt: skip
    ]
    assert not list(run_dir.glob("*.tmp"))


def test_finish_and_fail_rewrite_the_file(tmp_path: Path) -> None:
    record, run_dir = start_run(tmp_path, 1, 1, "ok", AnalysisConfig(), selection())
    record.outputs.append("a.csv")
    record.finish(run_dir)
    done = json.loads((run_dir / RUN_FILE).read_text())
    assert done["status"] == DONE and done["error"] is None and done["finished_at"]
    assert done["outputs"] == ["a.csv"]

    record, run_dir = start_run(tmp_path, 1, 1, "bad", AnalysisConfig(), selection())
    record.finish(run_dir, error="no included sessions")
    failed = json.loads((run_dir / RUN_FILE).read_text())
    assert failed["status"] == FAILED and failed["error"] == "no included sessions"


def test_git_hash_is_null_when_the_build_did_not_record_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GIT_SHA", raising=False)
    _, run_dir = start_run(tmp_path, 1, 1, "r", AnalysisConfig(), selection())
    assert json.loads((run_dir / RUN_FILE).read_text())["git_hash"] is None


def test_a_run_id_is_never_reused(tmp_path: Path) -> None:
    start_run(tmp_path, 1, 1, "same", AnalysisConfig(), selection())
    with pytest.raises(FileExistsError):
        start_run(tmp_path, 1, 1, "same", AnalysisConfig(), selection())
