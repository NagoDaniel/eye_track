"""Gaze prediction and validity (PLAN §8.7): apply the calibration model to every frame.

`x_px` and `y_px` are the raw prediction in viewport CSS px, for every frame with usable features
(a face, no blink, every feature finite). A prediction is `valid` when the frame is usable and the
prediction lies within the viewport expanded by `valid_viewport_margin` on every side. The viewport
is the one logged by the last `viewport_change` event before the frame.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from numpy.typing import NDArray

from app.events import parse_events
from app.schemas import EventRecord
from app.storage import derived_dir, events_path
from worker.pipeline.calibration import CALIBRATION_FILE
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURE_VECTOR_COLUMNS, FEATURES_FILE
from worker.pipeline.ridge import RidgeModel, predict
from worker.pipeline.sync import SYNC_FILE, read_t_of_pts

Floats = NDArray[np.float64]
Bools = NDArray[np.bool_]


@dataclass(frozen=True)
class ViewportTimeline:
    """The viewport size over time, from the `viewport_change` events."""

    t_ms: Floats
    width: Floats
    height: Floats

    def at(self, t_ms: Floats) -> tuple[Floats, Floats]:
        """Size in force at each time; before the first event, the first size."""
        k = np.clip(np.searchsorted(self.t_ms, t_ms, side="right") - 1, 0, len(self.t_ms) - 1)
        return self.width[k], self.height[k]


def viewport_timeline(events: Sequence[EventRecord]) -> ViewportTimeline:
    changes = [e for e in events if e.type == "viewport_change"]
    if not changes:
        raise ValueError("the session has no viewport_change event")
    return ViewportTimeline(
        t_ms=np.array([e.t for e in changes]),
        width=np.array([float(e.data["viewport"]["width"]) for e in changes]),
        height=np.array([float(e.data["viewport"]["height"]) for e in changes]),
    )


@dataclass(frozen=True)
class GazePrediction:
    x_px: Floats  # NaN where the frame is not usable
    y_px: Floats
    valid: Bools
    viewport_width: Floats
    viewport_height: Floats


def predict_gaze(
    model: RidgeModel,
    vectors: Floats,
    usable: Bools,
    t_ms: Floats,
    viewport: ViewportTimeline,
    config: PipelineConfig,
) -> GazePrediction:
    """Predict for every usable row of `vectors` (frames, features) and decide validity."""
    x = np.full(len(vectors), np.nan)
    y = np.full(len(vectors), np.nan)
    if usable.any():
        xy = predict(model, vectors[usable])
        x[usable], y[usable] = xy[:, 0], xy[:, 1]
    width, height = viewport.at(t_ms)
    margin = config.valid_viewport_margin
    with np.errstate(invalid="ignore"):  # NaN compares false: unusable frames are not valid
        inside = (
            (x >= -margin * width)
            & (x <= (1 + margin) * width)
            & (y >= -margin * height)
            & (y <= (1 + margin) * height)
        )
    return GazePrediction(x, y, usable & inside, width, height)


@dataclass(frozen=True)
class SessionPrediction:
    """The frames of a session with their client time and prediction."""

    index: NDArray[np.int64]
    pts: Floats
    t_ms: Floats
    face: Bools
    blink: Bools
    usable: Bools
    gaze: GazePrediction


def predict_session(
    root: Path, study_id: int, session_id: uuid.UUID, config: PipelineConfig
) -> SessionPrediction:
    """Read the session's derived files and predict. ValueError when the calibration failed."""
    derived = derived_dir(root, study_id, session_id)
    calibration = json.loads((derived / CALIBRATION_FILE).read_text(encoding="utf-8"))
    if calibration["status"] != "ok":
        raise ValueError(
            f"the calibration failed ({calibration['failed_reason']}): nothing to predict"
        )
    if calibration["feature_columns"] != list(FEATURE_VECTOR_COLUMNS):
        raise ValueError("the calibration was fitted on other features than this code computes")
    model = RidgeModel.from_dict(calibration["model"])

    table = pq.read_table(derived / FEATURES_FILE)
    a_ms, b = read_t_of_pts(derived / SYNC_FILE)
    pts = table["pts"].to_numpy()
    t_ms = a_ms + 1000.0 * b * pts
    vectors = np.stack(
        [table[c].to_numpy(zero_copy_only=False) for c in FEATURE_VECTOR_COLUMNS], axis=1
    )
    blink = np.asarray(table["blink"].to_pylist(), dtype=np.bool_)
    face = np.asarray(table["face"].to_pylist(), dtype=np.bool_)
    usable = np.isfinite(vectors).all(axis=1) & ~blink
    events = parse_events(events_path(root, study_id, session_id).read_bytes())
    gaze = predict_gaze(model, vectors, usable, t_ms, viewport_timeline(events), config)
    return SessionPrediction(
        index=table["index"].to_numpy(), pts=pts, t_ms=t_ms, face=face, blink=blink,
        usable=usable, gaze=gaze,
    )  # fmt: skip
