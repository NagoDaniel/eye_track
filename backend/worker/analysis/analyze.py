"""One `analyze_video` run from the selected sessions to the output files (PLAN §9).

Stages run in order (heatmap video, static heatmaps, AOI metrics, synchrony); each adds its files to
`run.json` as soon as it is done, so a failing stage leaves the finished ones in place and the run
marked `failed` with the stage in its error (PLAN §1: fail loudly).
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.aois import AoiFile
from app.storage import video_path
from worker.analysis.aoi_metrics import aoi_metrics, write_metrics
from worker.analysis.config import AnalysisConfig
from worker.analysis.density import StimSamples, by_stim_time, choose_sigma
from worker.analysis.heatmap_static import render_static, segments_for
from worker.analysis.heatmap_video import render_heatmap
from worker.analysis.load import SessionGaze, load_session
from worker.analysis.run import RunRecord
from worker.analysis.select import Excluded, Selection
from worker.analysis.synchrony import synchrony, write_synchrony
from worker.cancel import JobCancelled, check_cancelled

log = logging.getLogger("worker")

GAZE_UNREADABLE = "gaze_unreadable"
NO_VALID_SAMPLES = "no_valid_samples"


class AnalysisError(Exception):
    """The run cannot produce anything (a data problem, not a crash)."""


@dataclass(frozen=True)
class VideoInfo:
    id: int
    duration_s: float
    width: int
    height: int


def load_included(
    root: Path,
    study_id: int,
    video_id: int,
    selection: Selection,
    config: AnalysisConfig,
    record: RunRecord,
) -> list[SessionGaze]:
    """Read the gaze of the included sessions. One that cannot be read, or has no valid sample on
    the video, moves to the excluded list with the reason, and the run goes on."""
    loaded: list[SessionGaze] = []
    kept: list[dict[str, Any]] = []
    for info in selection.included:
        try:
            gaze = load_session(root, study_id, info.id, info.participant_code, video_id, config)
        except (OSError, ValueError, KeyError) as exc:
            log.error("gaze of a session could not be read", extra={"session_id": str(info.id)})
            record.excluded.append(
                Excluded(info.id, info.participant_code, [f"{GAZE_UNREADABLE}: {exc}"]).as_dict()
            )
            continue
        if len(gaze.samples.t_ms) == 0:
            record.excluded.append(
                Excluded(info.id, info.participant_code, [NO_VALID_SAMPLES]).as_dict()
            )
            continue
        loaded.append(gaze)
        kept.append({
            "session_id": str(info.id),
            "participant_code": info.participant_code,
            "samples": len(gaze.samples.t_ms),
            "fixations": len(gaze.fixations.start_t),
            "accuracy_px": gaze.accuracy_px,
            "video_width_px": gaze.video_width_px,
        })  # fmt: skip
    record.included = kept
    return loaded


def analyze(
    root: Path,
    run_dir: Path,
    record: RunRecord,
    video: VideoInfo,
    aois: AoiFile,
    sessions: list[SessionGaze],
) -> None:
    """Run the stages. Raises AnalysisError, or whatever a stage raises (named in the record)."""
    config = record.config
    if not sessions:
        raise AnalysisError("no included sessions with gaze on this video (see `excluded`)")
    file = video_path(root, video.id)
    if not file.is_file():
        raise AnalysisError(f"the stimulus video is not on this machine ({file})")

    sigma = choose_sigma(sessions, config)
    record.results["sigma"] = sigma.as_dict()
    people: list[tuple[str, StimSamples]] = [
        (s.participant_code, by_stim_time(s.samples)) for s in sessions
    ]
    samples = [p for _, p in people]

    def heatmap_video() -> dict[str, Any]:
        summary = render_heatmap(file, run_dir, samples, sigma.value, config)
        return {"files": ["heatmap.mp4", "density.npz"], "summary": summary.as_dict()}

    def heatmap_static() -> dict[str, Any]:
        segments = segments_for(aois.segments, video.duration_s, config)
        summary = render_static(file, run_dir, samples, sigma.value, segments, config)
        return {"files": summary.files, "summary": summary.as_dict()}

    def metrics() -> dict[str, Any]:
        if not aois.aois:
            return {"files": [], "summary": {"skipped": "the video has no AOIs"}}
        rows = aoi_metrics(
            sessions,
            aois.aois,
            config.aoi_tolerance_k,
            video.duration_s,
            video.width / video.height,
        )
        files = write_metrics(run_dir, rows)
        unknown = [s.participant_code for s in sessions if s.accuracy_norm is None]
        return {
            "files": files,
            "summary": {
                "aois": len(aois.aois),
                "rows": len(rows),
                "tolerance_k": config.aoi_tolerance_k,
                "tolerance_without_accuracy": unknown if config.aoi_tolerance_k > 0 else [],
            },
        }

    def sync() -> dict[str, Any]:
        result = synchrony(
            people, video.duration_s, video.width / video.height, sigma.value, config
        )
        return {"files": write_synchrony(run_dir, result), "summary": result.summary()}

    stages: list[tuple[str, Callable[[], dict[str, Any]]]] = [
        ("heatmap_video", heatmap_video),
        ("heatmap_static", heatmap_static),
        ("aoi_metrics", metrics),
        ("synchrony", sync),
    ]
    for name, stage in stages:
        check_cancelled(force=True)
        started = time.perf_counter()
        try:
            out = stage()
        except JobCancelled:
            raise
        except Exception as exc:
            raise AnalysisError(f"{name}: {exc}") from exc
        record.outputs.extend(out["files"])
        record.results[name] = {**out["summary"], "seconds": time.perf_counter() - started}
        record.write(run_dir)
        log.info("analysis stage done", extra={"stage": name, "run_id": record.run_id})
