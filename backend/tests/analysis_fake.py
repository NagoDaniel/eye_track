"""A finished analysis run on disk without running the analysis (for the admin and ZIP tests)."""

from pathlib import Path

from app.analysis_runs import new_run_id
from tests.test_analysis_select import info
from worker.analysis.config import AnalysisConfig
from worker.analysis.run import start_run
from worker.analysis.select import Excluded, Selection

FILES = {
    "heatmap.mp4": b"\x00\x00\x00\x18ftypmp42" + b"v" * 200,
    "density.npz": b"PK" + b"d" * 100,
    "static/001_window_1.png": b"\x89PNG\r\n\x1a\n" + b"p" * 50,
    "aoi_metrics_wide.csv": b"participant_code,aoi_id,dwell_sample_s\nP1,a,1.5\n",
    "aoi_metrics_long.csv": b"participant_code,aoi_id,metric,value\nP1,a,dwell_sample_s,1.5\n",
    "synchrony.csv": b"window_start_s,window_end_s,participants,dispersion,nss_mean\n",
    "synchrony.png": b"\x89PNG\r\n\x1a\n" + b"s" * 50,
}


def make_run(
    root: Path,
    study_id: int = 1,
    video_id: int = 1,
    files: dict[str, bytes] | None = None,
    error: str | None = None,
    codes: tuple[str, ...] = ("P1",),
    run_id: str | None = None,
) -> str:
    """Write a run directory with `files` listed as outputs; returns the run id."""
    run_id = run_id or new_run_id()
    selection = Selection(
        included=[info(n + 1, code) for n, code in enumerate(codes)],
        excluded=[Excluded(info(99).id, "Z9", ["not_processed"])],
    )
    record, run_dir = start_run(root, study_id, video_id, run_id, AnalysisConfig(), selection)
    for name, data in (FILES if files is None else files).items():
        (run_dir / name).parent.mkdir(parents=True, exist_ok=True)
        (run_dir / name).write_bytes(data)
        record.outputs.append(name)
    record.results["synchrony"] = {
        "windows": 5,
        "windows_with_two_or_more": 3,
        "mean_dispersion": 0.125,
        "mean_nss": 0.5,
        "nss_by_participant": {code: 0.5 for code in codes},
    }
    record.finish(run_dir, error=error)
    return run_id
