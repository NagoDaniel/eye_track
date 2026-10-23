"""Validation accuracy, precision and drift (PLAN §8.8).

A validation block is one run of validation-phase dots (5 points). For each point the predictions of
the valid frames in the dot's window (700 ms before to 100 ms after its click, like the training
window) give
- accuracy: the distance between the median prediction and the target;
- precision: the RMS of the sample-to-sample distance (consecutive frames only).
Both are in px and as `error_norm` (px over the viewport diagonal). Degrees of visual angle need a
station (lab mode); none exists yet, so `error_deg` is always null (DECISIONS milestone 3 item 7).

The mean offset vector (prediction minus target) of a block is the drift at the block's time. The
offset at any time is the linear interpolation between the blocks' offsets, constant before the
first and after the last (one block: constant); subtracting it gives the drift-corrected gaze. The
accuracy is reported with and without that correction.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from app.events import parse_events
from app.schemas import EventRecord
from app.storage import derived_dir, events_path
from worker.pipeline.calibration import Dot, build_dots
from worker.pipeline.config import PipelineConfig
from worker.pipeline.predict import (
    SessionPrediction,
    ViewportTimeline,
    predict_session,
    viewport_timeline,
)
from worker.pipeline.spec import SPEC_A, PipelineSpec

Floats = NDArray[np.float64]
Bools = NDArray[np.bool_]


# --- Blocks, from the events --------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationDots:
    index: int  # position among the session's validation blocks
    kind: str  # before_video, between_videos or after_last_video
    start_t: float
    dots: list[Dot]


def _kind(start_t: float, events: Sequence[EventRecord]) -> str:
    """Where in the session a block is, from the video events."""
    loads = [e.t for e in events if e.type == "video_load"]
    ends = [e.t for e in events if e.type == "video_ended"]
    started = sum(1 for t in loads if t < start_t)
    if started == 0:
        return "before_video"
    if started == len(loads) and sum(1 for t in ends if t < start_t) >= len(loads):
        return "after_last_video"
    return "between_videos"


def validation_blocks(events: Sequence[EventRecord], since_t: float = 0.0) -> list[ValidationDots]:
    """The validation blocks that start at or after `since_t`. A block is a run of validation-phase
    dot events; calibration dots, a gate result and the start or end of a video close it."""
    blocks: list[ValidationDots] = []
    shows: dict[int, tuple[float, float]] = {}
    clicks: dict[int, float] = {}
    start: float | None = None

    def close() -> None:
        nonlocal shows, clicks, start
        if start is not None and start >= since_t:
            blocks.append(
                ValidationDots(len(blocks), _kind(start, events), start, build_dots(shows, clicks))
            )
        shows, clicks, start = {}, {}, None

    for event in events:
        if event.type in ("calib_dot_show", "calib_dot_click", "calib_dot_hide"):
            if event.data.get("phase") != "validation":
                close()
                continue
            if start is None:
                start = event.t
            dot_id = int(event.data["id"])
            if event.type == "calib_dot_show":
                shows[dot_id] = (float(event.data["x"]), float(event.data["y"]))
            elif event.type == "calib_dot_click":
                clicks[dot_id] = event.t
        elif event.type in ("calib_gate_result", "video_load", "video_ended"):
            close()
    close()
    return blocks


# --- Scores -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PointScore:
    id: int
    x: float
    y: float
    click_t: float | None
    frames: int  # valid frames in the window
    median_x: float | None
    median_y: float | None
    error_px: float | None
    error_norm: float | None
    error_corrected_px: float | None
    error_corrected_norm: float | None
    precision_px: float | None  # RMS of the sample-to-sample distance; None with < 1 pair

    @property
    def offset(self) -> tuple[float, float] | None:
        if self.median_x is None or self.median_y is None:
            return None
        return self.median_x - self.x, self.median_y - self.y


def _mean(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return float(np.mean(present)) if present else None


@dataclass(frozen=True)
class BlockScore:
    index: int
    kind: str
    t_ms: float | None  # mean click time of the points that have data
    points: list[PointScore]

    @property
    def offset(self) -> tuple[float, float] | None:
        offsets = [o for p in self.points if (o := p.offset) is not None]
        if not offsets:
            return None
        return float(np.mean([o[0] for o in offsets])), float(np.mean([o[1] for o in offsets]))

    def as_dict(self) -> dict[str, Any]:
        offset = self.offset
        return {
            "index": self.index,
            "kind": self.kind,
            "t_ms": self.t_ms,
            "points_with_data": sum(1 for p in self.points if p.error_px is not None),
            "mean_error_px": _mean([p.error_px for p in self.points]),
            "mean_error_norm": _mean([p.error_norm for p in self.points]),
            "mean_error_corrected_px": _mean([p.error_corrected_px for p in self.points]),
            "mean_error_corrected_norm": _mean([p.error_corrected_norm for p in self.points]),
            "mean_error_deg": None,
            "precision_px": _mean([p.precision_px for p in self.points]),
            "offset_px": None if offset is None else {"x": offset[0], "y": offset[1]},
            "points": [
                {
                    "id": p.id,
                    "x": p.x,
                    "y": p.y,
                    "click_t": p.click_t,
                    "frames": p.frames,
                    "median_x": p.median_x,
                    "median_y": p.median_y,
                    "error_px": p.error_px,
                    "error_norm": p.error_norm,
                    "error_deg": None,
                    "error_corrected_px": p.error_corrected_px,
                    "error_corrected_norm": p.error_corrected_norm,
                    "precision_px": p.precision_px,
                }
                for p in self.points
            ],  # fmt: skip
        }

    @property
    def precision_px(self) -> float | None:
        return _mean([p.precision_px for p in self.points])


@dataclass(frozen=True)
class DriftModel:
    """The offset (prediction minus target) at the blocks' times, interpolated in between."""

    t_ms: Floats
    dx: Floats
    dy: Floats

    def offset_at(self, t_ms: Floats) -> tuple[Floats, Floats]:
        return np.interp(t_ms, self.t_ms, self.dx), np.interp(t_ms, self.t_ms, self.dy)

    def correct(self, t_ms: Floats, x: Floats, y: Floats) -> tuple[Floats, Floats]:
        """The gaze with the drift at each time subtracted."""
        dx, dy = self.offset_at(t_ms)
        return x - dx, y - dy


@dataclass(frozen=True)
class ValidationResult:
    blocks: list[BlockScore]
    drift: DriftModel | None  # None when no block has data
    precision_px: float | None  # median over the blocks of their precision (for I-DT, §8.9)

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocks": [b.as_dict() for b in self.blocks],
            "precision_px": self.precision_px,
            "drift": None
            if self.drift is None
            else {
                "t_ms": self.drift.t_ms.tolist(),
                "dx_px": self.drift.dx.tolist(),
                "dy_px": self.drift.dy.tolist(),
            },
        }


def _point(
    dot: Dot,
    t_ms: Floats,
    x: Floats,
    y: Floats,
    valid: Bools,
    viewport: ViewportTimeline,
    config: PipelineConfig,
) -> PointScore:
    empty = PointScore(
        dot.id, dot.x, dot.y, dot.click_t, 0, None, None, None, None, None, None, None
    )
    if dot.click_t is None:
        return empty
    lo = int(np.searchsorted(t_ms, dot.click_t - config.calib_window_before_click_ms))
    hi = int(np.searchsorted(t_ms, dot.click_t + config.calib_window_after_click_ms, "right"))
    rows = lo + np.flatnonzero(valid[lo:hi])
    if len(rows) == 0:
        return empty
    mx, my = float(np.median(x[rows])), float(np.median(y[rows]))
    width, height = viewport.at(np.array([dot.click_t]))
    error = float(np.hypot(mx - dot.x, my - dot.y))
    adjacent = rows[1:] == rows[:-1] + 1  # only frames that follow each other
    steps = np.hypot(np.diff(x[rows]), np.diff(y[rows]))[adjacent]
    precision = float(np.sqrt(np.mean(steps**2))) if len(steps) else None
    return PointScore(
        dot.id, dot.x, dot.y, dot.click_t, len(rows), mx, my, error,
        error / float(np.hypot(width[0], height[0])), None, None, precision,
    )  # fmt: skip


def score_validation(
    blocks: Sequence[ValidationDots],
    t_ms: Floats,
    x_px: Floats,
    y_px: Floats,
    valid: Bools,
    viewport: ViewportTimeline,
    config: PipelineConfig,
) -> ValidationResult:
    """Score every block on the raw predictions of the valid frames (`t_ms` increasing)."""
    raw = [
        BlockScore(
            b.index, b.kind, None,
            [_point(d, t_ms, x_px, y_px, valid, viewport, config) for d in b.dots],
        )
        for b in blocks
    ]  # fmt: skip
    timed = []
    for block in raw:
        clicks = [
            p.click_t for p in block.points if p.error_px is not None and p.click_t is not None
        ]
        timed.append(
            BlockScore(
                block.index, block.kind, float(np.mean(clicks)) if clicks else None, block.points
            )
        )
    with_data = [b for b in timed if b.t_ms is not None and b.offset is not None]
    if not with_data:
        return ValidationResult(timed, None, None)
    drift = DriftModel(
        t_ms=np.array([b.t_ms for b in with_data], dtype=np.float64),
        dx=np.array([b.offset[0] for b in with_data if b.offset]),
        dy=np.array([b.offset[1] for b in with_data if b.offset]),
    )
    # The same points, with the drift at their time taken out of the median prediction.
    corrected: list[BlockScore] = []
    for block in timed:
        points = []
        for p in block.points:
            if p.median_x is None or p.median_y is None or p.click_t is None:
                points.append(p)
                continue
            dx, dy = drift.offset_at(np.array([p.click_t]))
            error = float(np.hypot(p.median_x - dx[0] - p.x, p.median_y - dy[0] - p.y))
            width, height = viewport.at(np.array([p.click_t]))
            norm = error / float(np.hypot(width[0], height[0]))
            points.append(PointScore(
                p.id, p.x, p.y, p.click_t, p.frames, p.median_x, p.median_y, p.error_px,
                p.error_norm, error, norm, p.precision_px,
            ))  # fmt: skip
        corrected.append(BlockScore(block.index, block.kind, block.t_ms, points))
    precisions = [b.precision_px for b in corrected if b.precision_px is not None]
    return ValidationResult(corrected, drift, float(np.median(precisions)) if precisions else None)


def validate_session(
    root: Path,
    study_id: int,
    session_id: uuid.UUID,
    config: PipelineConfig,
    prediction: SessionPrediction | None = None,
    spec: PipelineSpec = SPEC_A,
) -> ValidationResult:
    """Score the session's validation blocks from the calibration attempt on. The predictions are
    made here unless the caller already has them."""
    derived = derived_dir(root, study_id, session_id)
    calibration = json.loads((derived / spec.calibration_file).read_text(encoding="utf-8"))
    if calibration["status"] != "ok":
        raise ValueError(
            f"the calibration failed ({calibration['failed_reason']}): nothing to score"
        )
    since = float(calibration["attempt_used"]["start_t"])
    events = parse_events(events_path(root, study_id, session_id).read_bytes())
    prediction = prediction or predict_session(root, study_id, session_id, config, spec)
    gaze = prediction.gaze
    return score_validation(
        validation_blocks(events, since), prediction.t_ms, gaze.x_px, gaze.y_px, gaze.valid,
        viewport_timeline(events), config,
    )  # fmt: skip
