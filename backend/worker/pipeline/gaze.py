"""Assemble `gaze_{a,b}.parquet` and `fixations_{a,b}.parquet` (PLAN §7, §8.7 to §8.9).

Runs prediction, smoothing, validation/drift, the stimulus-time map and the fixation detection, and
writes one row per webcam frame (`gaze_a.parquet`) and one row per fixation (`fixations_a.parquet`).
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from numpy.typing import NDArray

from app.events import parse_events, parse_records
from app.schemas import EventRecord, StimulusFrameRecord
from app.storage import atomic_writer, derived_dir, events_path, stimulus_frames_path
from worker.pipeline.config import PipelineConfig
from worker.pipeline.fixations import FixationParams, all_runs_fixations, runs
from worker.pipeline.predict import predict_session
from worker.pipeline.smoothing import smooth_gaze
from worker.pipeline.spec import SPEC_A, PipelineSpec
from worker.pipeline.stimulus_map import StimulusMap, VideoRectTimeline
from worker.pipeline.validation import validate_session

GAZE_FILE = SPEC_A.gaze_file  # Pipeline A's files; `PipelineSpec` names the others
FIXATIONS_FILE = SPEC_A.fixations_file
GAZE_VERSION = 1
PHASES = ("calibration", "validation", "playback", "other")

Floats = NDArray[np.float64]
Bools = NDArray[np.bool_]


def phase_intervals(events: Sequence[EventRecord]) -> list[tuple[float, float, str]]:
    """The time span of every run of calibration or validation dots: `(first, last, phase)`.
    A run ends when the other kind of dot, a gate result or a video event comes."""
    spans: list[tuple[float, float, str]] = []
    current: tuple[float, float, str] | None = None
    for event in events:
        if event.type in ("calib_dot_show", "calib_dot_click", "calib_dot_hide"):
            phase = str(event.data.get("phase"))
            if current is not None and current[2] != phase:
                spans.append(current)
                current = None
            current = (current[0] if current else event.t, event.t, phase)
        elif event.type in ("calib_gate_result", "video_load", "video_ended") and current:
            spans.append(current)
            current = None
    if current is not None:
        spans.append(current)
    return spans


def frame_phases(
    t_ms: Floats, intervals: Sequence[tuple[float, float, str]], on_screen: Bools
) -> NDArray[np.object_]:
    """`calibration` / `validation` inside a dot run, else `playback` while a video is on screen,
    else `other`."""
    phase = np.where(on_screen, "playback", "other").astype(object)
    for start, end, name in intervals:
        phase[(t_ms >= start) & (t_ms <= end)] = name
    return phase


def confidence(
    x: Floats,
    y: Floats,
    valid: Bools,
    interpolated: Bools,
    width: Floats,
    height: Floats,
    config: PipelineConfig,
) -> Floats:
    """0 for an invalid sample, 0.5 for an interpolated one, otherwise 1 minus a linear penalty for
    the prediction lying outside the viewport (0 at its edge, 1 at the validity limit)
    (DECISIONS milestone 3 item 5)."""
    with np.errstate(invalid="ignore"):
        over_x = np.maximum(np.maximum(-x, x - width), 0) / width
        over_y = np.maximum(np.maximum(-y, y - height), 0) / height
        penalty = np.maximum(over_x, over_y) / config.valid_viewport_margin
    conf = np.where(valid, np.clip(1 - penalty, 0, 1), 0.0)
    return np.where(interpolated & ~valid, 0.5, conf)


@dataclass(frozen=True)
class GazeSummary:
    frames: int
    valid: int
    playback_frames: int
    valid_playback: int
    fixations: int
    drift_corrected: bool
    validation_precision_px: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f32(values: Floats, mask: Bools | None = None) -> pa.Array:
    if mask is None:
        return pa.array(values.astype(np.float32), pa.float32())
    return pa.array(values.astype(np.float32), pa.float32(), mask=mask)


def _nullable(values: Floats) -> pa.Array:
    return _f32(values, np.isnan(values))


def run_gaze(
    root: Path,
    study_id: int,
    session_id: uuid.UUID,
    config: PipelineConfig,
    spec: PipelineSpec = SPEC_A,
) -> GazeSummary:
    derived = derived_dir(root, study_id, session_id)
    events = parse_events(events_path(root, study_id, session_id).read_bytes())
    frames_path = stimulus_frames_path(root, study_id, session_id)
    stimulus_frames = (
        parse_records(frames_path.read_bytes(), StimulusFrameRecord, "stimulus_frames")
        if frames_path.exists()
        else []
    )

    prediction = predict_session(root, study_id, session_id, config, spec)
    gaze, t = prediction.gaze, prediction.t_ms
    smoothed = smooth_gaze(t, gaze.x_px, gaze.y_px, gaze.valid, config)
    validation = validate_session(root, study_id, session_id, config, prediction, spec)
    drift = validation.drift

    lookup = StimulusMap(stimulus_frames, events, config).lookup(t)
    video_id = np.array(lookup.video_id, dtype=object)
    on_screen = np.array([v is not None for v in lookup.video_id], dtype=np.bool_)
    phase = frame_phases(t, phase_intervals(events), on_screen)
    rects = VideoRectTimeline(events)
    x_vid, y_vid = rects.normalise(t, smoothed.x, smoothed.y)

    nan = np.full(len(t), np.nan)
    x_sm_dc, y_sm_dc = (nan, nan) if drift is None else drift.correct(t, smoothed.x, smoothed.y)
    x_vid_dc, y_vid_dc = (nan, nan) if drift is None else rects.normalise(t, x_sm_dc, y_sm_dc)
    x_px_dc, y_px_dc = (nan, nan) if drift is None else drift.correct(t, gaze.x_px, gaze.y_px)

    conf = confidence(
        gaze.x_px, gaze.y_px, gaze.valid, smoothed.interpolated,
        gaze.viewport_width, gaze.viewport_height, config,
    )  # fmt: skip
    params = {
        "version": GAZE_VERSION,
        "pipeline": spec.id,
        "config": config.as_dict(),
        "drift_corrected": drift is not None,
        "validation_precision_px": validation.precision_px,
    }
    table = pa.Table.from_arrays(
        [
            pa.array(t, pa.float64()),
            pa.array(prediction.pts, pa.float64()),
            pa.array([v for v in video_id], pa.string()),
            pa.array(lookup.stim_time, pa.float64(), mask=np.isnan(lookup.stim_time)),
            _f32(gaze.x_px), _f32(gaze.y_px), _f32(smoothed.x), _f32(smoothed.y),
            _nullable(x_vid), _nullable(y_vid),
            pa.array(gaze.valid, pa.bool_()), pa.array(prediction.blink, pa.bool_()),
            _f32(conf), pa.array(list(phase), pa.string()),
            pa.array(smoothed.interpolated, pa.bool_()),
            _nullable(x_px_dc), _nullable(y_px_dc),
            _nullable(x_sm_dc), _nullable(y_sm_dc), _nullable(x_vid_dc), _nullable(y_vid_dc),
        ],
        names=[
            "t", "webcam_pts", "video_id", "stim_time", "x_px", "y_px", "x_sm", "y_sm",
            "x_vid", "y_vid", "valid", "blink", "conf", "phase", "interpolated",
            "x_px_dc", "y_px_dc", "x_sm_dc", "y_sm_dc", "x_vid_dc", "y_vid_dc",
        ],
    ).replace_schema_metadata({"gaze": json.dumps(params)})  # fmt: skip
    with atomic_writer(derived / spec.gaze_file) as target:
        pq.write_table(table, target, compression="snappy")

    # Fixations: smoothed samples (valid or interpolated) of the playback phase.
    playback = (phase == "playback") & np.isfinite(smoothed.x)
    fixation_params = FixationParams.of(config, validation.precision_px)
    found = all_runs_fixations(
        runs(prediction.index, playback, video_id, gaze.viewport_width, gaze.viewport_height),
        t, smoothed.x, smoothed.y, gaze.viewport_width, fixation_params, config,
    )  # fmt: skip
    _write_fixations(derived / spec.fixations_file, found, t, smoothed, lookup.stim_time, video_id,
                     (x_vid, y_vid), (x_sm_dc, y_sm_dc), (x_vid_dc, y_vid_dc), fixation_params,
                     config)  # fmt: skip
    return GazeSummary(
        frames=len(t),
        valid=int(gaze.valid.sum()),
        playback_frames=int((phase == "playback").sum()),
        valid_playback=int((gaze.valid & (phase == "playback")).sum()),
        fixations=len(found),
        drift_corrected=drift is not None,
        validation_precision_px=validation.precision_px,
    )


def _write_fixations(
    path: Path,
    found: Sequence[NDArray[np.int64]],
    t: Floats,
    smoothed: Any,
    stim_time: Floats,
    video_id: NDArray[np.object_],
    vid: tuple[Floats, Floats],
    corrected: tuple[Floats, Floats],
    corrected_vid: tuple[Floats, Floats],
    params: FixationParams,
    config: PipelineConfig,
) -> None:
    def mean(values: Floats) -> list[float | None]:
        out: list[float | None] = []
        for rows in found:
            v = values[rows]
            out.append(None if np.isnan(v).all() else float(np.nanmean(v)))
        return out

    columns: dict[str, pa.Array] = {
        "id": pa.array(range(len(found)), pa.int64()),
        "start_t": pa.array([float(t[r[0]]) for r in found], pa.float64()),
        "end_t": pa.array([float(t[r[-1]]) for r in found], pa.float64()),
        "duration_ms": pa.array([float(t[r[-1]] - t[r[0]]) for r in found], pa.float64()),
        "samples": pa.array([len(r) for r in found], pa.int64()),
        "video_id": pa.array([video_id[r[0]] for r in found], pa.string()),
        "stim_start": pa.array(
            [None if np.isnan(stim_time[r[0]]) else float(stim_time[r[0]]) for r in found],
            pa.float64(),
        ),
        "stim_end": pa.array(
            [None if np.isnan(stim_time[r[-1]]) else float(stim_time[r[-1]]) for r in found],
            pa.float64(),
        ),
        "dispersion_px": pa.array(
            [float(np.ptp(smoothed.x[r]) + np.ptp(smoothed.y[r])) for r in found], pa.float64()
        ),
    }
    for name, values in (
        ("cx_px", smoothed.x), ("cy_px", smoothed.y), ("cx_vid", vid[0]), ("cy_vid", vid[1]),
        ("cx_px_dc", corrected[0]), ("cy_px_dc", corrected[1]),
        ("cx_vid_dc", corrected_vid[0]), ("cy_vid_dc", corrected_vid[1]),
    ):  # fmt: skip
        columns[name] = pa.array(mean(values), pa.float64())
    metadata = {
        "fixations": json.dumps(
            {
                "version": GAZE_VERSION,
                "algorithm": "I-DT",
                "dispersion_width_fraction": params.dispersion_fraction,
                "precision_factor": params.precision_factor,
                "validation_precision_px": params.validation_precision_px,
                "min_duration_ms": params.min_duration_ms,
                "config": config.as_dict(),
            }
        )
    }
    table = pa.table(columns).replace_schema_metadata(metadata)
    with atomic_writer(path) as target:
        pq.write_table(table, target, compression="snappy")
