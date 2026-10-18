"""Calibration fit, per session (PLAN §5.5, §8.6): from the calibration dots to a gaze model.

1. The events say which dots were shown, where, and when each was clicked, in attempts (one
   calibration block each). The attempt used is the last one that passed the browser gate, else the
   last one (DECISIONS milestone 3 item 3: attempts are told apart by event order).
2. The training samples of a dot are the usable frames (a face, no blink) from 700 ms before its
   click to 100 ms after it; their target is the dot's position in viewport px.
3. Leave-one-dot-out cross-validation chooses the regularisation for a degree-2 and for a plain
   linear ridge model (`ridge.py`); the one with the lower error is kept and both errors are
   recorded.
4. Samples whose prediction is far from their dot's median prediction are dropped and the model is
   chosen and fitted again on the rest.
5. If more than 3 dots have fewer than 5 usable frames the calibration has `failed` and there is no
   model (DECISIONS milestone 3 item 15: a data problem, not a crash).

`calibration_a.json` holds the result; the model is applied by `predict.py`.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pyarrow.parquet as pq
from numpy.typing import NDArray

from app.events import parse_events
from app.schemas import CalibGateResultData, EventRecord
from app.storage import atomic_write_bytes, derived_dir, events_path
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURE_VECTOR_COLUMNS, FEATURES_FILE
from worker.pipeline.ridge import Degree, RidgeModel, fit_ridge, predict
from worker.pipeline.sync import SYNC_FILE, read_t_of_pts

CALIBRATION_FILE = "calibration_a.json"
CALIBRATION_VERSION = 1

Floats = NDArray[np.float64]
Ints = NDArray[np.int64]


# --- Attempts and dots, from the events ---


@dataclass(frozen=True)
class Dot:
    id: int
    x: float  # viewport px
    y: float
    click_t: float | None  # client clock, ms; None when it was never clicked


@dataclass(frozen=True)
class Attempt:
    index: int  # 0-based, in order
    start_t: float
    dots: list[Dot]
    gate: CalibGateResultData | None  # the browser's pre-video verdict, if any

    @property
    def number(self) -> int:
        """The attempt number the browser logged, else the position in the session (1-based)."""
        return self.gate.attempt if self.gate is not None else self.index + 1

    @property
    def passed(self) -> bool:
        return self.gate is not None and self.gate.passed is True


def build_dots(shows: dict[int, tuple[float, float]], clicks: dict[int, float]) -> list[Dot]:
    """Dots in id order from the last show (position) and last click (time) of each id."""
    return [Dot(i, x, y, clicks.get(i)) for i, (x, y) in sorted(shows.items())]


def calibration_attempts(events: Sequence[EventRecord]) -> list[Attempt]:
    """One `Attempt` per run of calibration-phase dot events, with the `before_video`
    `calib_gate_result` that follows it. A dot that was restarted (fullscreen left) counts with its
    last show and last click (DATA_FORMATS)."""
    attempts: list[Attempt] = []
    shows: dict[int, tuple[float, float]] = {}
    clicks: dict[int, float] = {}
    start: float | None = None

    def close() -> None:
        nonlocal shows, clicks, start
        if start is not None:
            attempts.append(Attempt(len(attempts), start, build_dots(shows, clicks), None))
        shows, clicks, start = {}, {}, None

    for event in events:
        if event.type in ("calib_dot_show", "calib_dot_click", "calib_dot_hide"):
            if event.data.get("phase") != "calibration":
                close()
                continue
            if start is None:
                start = event.t
            dot_id = int(event.data["id"])
            if event.type == "calib_dot_show":
                shows[dot_id] = (float(event.data["x"]), float(event.data["y"]))
            elif event.type == "calib_dot_click":
                clicks[dot_id] = event.t
        elif event.type == "calib_gate_result":
            close()
            result = CalibGateResultData.model_validate(event.data)
            if result.block == "before_video" and attempts and attempts[-1].gate is None:
                last = attempts[-1]
                attempts[-1] = Attempt(last.index, last.start_t, last.dots, result)
    close()
    return attempts


def choose_attempt(attempts: Sequence[Attempt]) -> tuple[Attempt, str] | None:
    """The last attempt that passed the browser gate, else the last attempt (PLAN §5.6, §8.6)."""
    if not attempts:
        return None
    if len(attempts) == 1:
        return attempts[0], "only_attempt"
    passed = [a for a in attempts if a.passed]
    if passed:
        return passed[-1], "last_passed"
    return attempts[-1], "none_passed_used_last"


# --- Training samples ---


@dataclass(frozen=True)
class TrainingSet:
    features: Floats  # (samples, columns)
    targets: Floats  # (samples, 2)
    dot_index: Ints  # (samples,) index into the attempt's dots
    valid_per_dot: list[int]


def training_samples(
    dots: Sequence[Dot],
    frame_t_ms: Floats,
    vectors: Floats,
    usable: NDArray[np.bool_],
    config: PipelineConfig,
) -> TrainingSet:
    """The usable frames inside each dot's training window. `frame_t_ms` must be increasing."""
    features: list[Floats] = []
    targets: list[Floats] = []
    index: list[Ints] = []
    counts: list[int] = []
    for k, dot in enumerate(dots):
        if dot.click_t is None:
            counts.append(0)
            continue
        lo = int(np.searchsorted(frame_t_ms, dot.click_t - config.calib_window_before_click_ms))
        hi = int(
            np.searchsorted(frame_t_ms, dot.click_t + config.calib_window_after_click_ms, "right")
        )
        rows = lo + np.flatnonzero(usable[lo:hi])
        counts.append(len(rows))
        features.append(vectors[rows])
        targets.append(np.tile([dot.x, dot.y], (len(rows), 1)))
        index.append(np.full(len(rows), k, dtype=np.int64))
    width = vectors.shape[1]
    return TrainingSet(
        features=np.concatenate(features) if features else np.empty((0, width)),
        targets=np.concatenate(targets) if targets else np.empty((0, 2)),
        dot_index=np.concatenate(index) if index else np.empty(0, dtype=np.int64),
        valid_per_dot=counts,
    )


# --- Model choice ---


@dataclass(frozen=True)
class CvResult:
    degree: Degree
    alpha: float
    error_px: float  # mean over the left-out dots of the mean per-frame distance to the dot


@dataclass(frozen=True)
class ModelSelection:
    linear: CvResult
    quadratic: CvResult
    chosen: CvResult


def cross_validate(
    features: Floats, targets: Floats, dot_index: Ints, degree: Degree, alpha: float
) -> float:
    """Leave-one-dot-out error in px; infinity when a fit is impossible."""
    held = np.unique(dot_index)
    if len(held) < 2:
        return float("inf")
    total = 0.0
    for dot in held:
        train = dot_index != dot
        try:
            model = fit_ridge(features[train], targets[train], alpha, degree)
        except (ValueError, np.linalg.LinAlgError):
            return float("inf")
        predicted = predict(model, features[~train])
        total += float(np.linalg.norm(predicted - targets[~train], axis=1).mean())
    return total / len(held)


def select_model(
    features: Floats, targets: Floats, dot_index: Ints, config: PipelineConfig
) -> ModelSelection:
    """Best alpha for each degree, then the better degree; ties go to the simpler model."""

    def best(degree: Degree) -> CvResult:
        result = CvResult(degree, config.calib_alphas[-1], float("inf"))
        for alpha in sorted(config.calib_alphas, reverse=True):  # ties: stronger regularisation
            error = cross_validate(features, targets, dot_index, degree, alpha)
            if error < result.error_px:
                result = CvResult(degree, alpha, error)
        return result

    linear, quadratic = best(1), best(2)
    return ModelSelection(
        linear, quadratic, quadratic if quadratic.error_px < linear.error_px else linear
    )


def inlier_mask(
    model: RidgeModel, features: Floats, dot_index: Ints, config: PipelineConfig
) -> NDArray[np.bool_]:
    """False for samples further than `calib_outlier_mad` MADs from their dot's median prediction.
    The MAD of a dot is the median of its samples' distances to that median prediction."""
    predicted = predict(model, features)
    keep = np.ones(len(features), dtype=np.bool_)
    for dot in np.unique(dot_index):
        rows = np.flatnonzero(dot_index == dot)
        distance = np.linalg.norm(predicted[rows] - np.median(predicted[rows], axis=0), axis=1)
        mad = float(np.median(distance))
        if mad > 0:
            keep[rows] = distance <= config.calib_outlier_mad * mad
    return keep


# --- The fit ---

Status = Literal["ok", "failed"]


@dataclass(frozen=True)
class CalibrationFit:
    status: Status
    failed_reason: str | None
    attempt: Attempt | None
    attempt_reason: str | None
    attempts: list[Attempt]
    valid_per_dot: list[int]
    kept_per_dot: list[int]
    model: RidgeModel | None
    selection: ModelSelection | None
    viewport: tuple[float, float] | None
    config: PipelineConfig

    def as_dict(self) -> dict[str, Any]:
        used = self.attempt
        diagonal = None if self.viewport is None else float(np.hypot(*self.viewport))
        selection = self.selection

        def cv(r: CvResult) -> dict[str, Any]:
            return {"degree": r.degree, "alpha": r.alpha, "error_px": r.error_px}

        return {
            "version": CALIBRATION_VERSION,
            "config": self.config.as_dict(),
            "status": self.status,
            "failed_reason": self.failed_reason,
            "feature_columns": list(FEATURE_VECTOR_COLUMNS),
            "attempt_used": None
            if used is None
            else {
                "attempt": used.number,
                "index": used.index,
                "passed": used.passed,
                "reason": self.attempt_reason,
                "start_t": used.start_t,
            },
            "attempts": [
                {
                    "attempt": a.number,
                    "index": a.index,
                    "passed": a.passed,
                    "gate_reason": None if a.gate is None else a.gate.reason,
                    "dots": len(a.dots),
                }
                for a in self.attempts
            ],
            "viewport": None
            if self.viewport is None
            else {"width": self.viewport[0], "height": self.viewport[1]},
            "dots": []
            if used is None
            else [
                {
                    "id": d.id,
                    "x": d.x,
                    "y": d.y,
                    "click_t": d.click_t,
                    "valid_frames": self.valid_per_dot[k],
                    "kept_frames": self.kept_per_dot[k],
                }
                for k, d in enumerate(used.dots)
            ],  # fmt: skip
            "samples": {
                "valid": sum(self.valid_per_dot),
                "kept": sum(self.kept_per_dot),
                "rejected": sum(self.valid_per_dot) - sum(self.kept_per_dot),
            },
            "cv": None
            if selection is None
            else {
                "linear": cv(selection.linear),
                "quadratic": cv(selection.quadratic),
                "chosen": "quadratic" if selection.chosen.degree == 2 else "linear",
                "error_px": selection.chosen.error_px,
                "error_norm": None if diagonal is None else selection.chosen.error_px / diagonal,
            },
            "model": None if self.model is None else self.model.as_dict(),
        }


def _failed(
    reason: str, attempts: list[Attempt], config: PipelineConfig, **rest: Any
) -> CalibrationFit:
    values: dict[str, Any] = {
        "attempt": None, "attempt_reason": None, "valid_per_dot": [], "kept_per_dot": [],
        "viewport": None,
    }  # fmt: skip
    values.update(rest)
    return CalibrationFit("failed", reason, attempts=attempts, model=None, selection=None,
                          config=config, **values)  # fmt: skip


def fit_calibration(
    events: Sequence[EventRecord],
    frame_t_ms: Floats,
    vectors: Floats,
    usable: NDArray[np.bool_],
    config: PipelineConfig,
) -> CalibrationFit:
    """The calibration of a session. Never raises for bad data: a calibration that cannot be fitted
    comes back as `failed` with the reason."""
    attempts = calibration_attempts(events)
    chosen = choose_attempt(attempts)
    if chosen is None:
        return _failed("no_calibration_dots", attempts, config)
    attempt, reason = chosen
    viewport = None
    for e in events:
        if e.t > attempt.start_t:
            break
        if e.type == "viewport_change":
            size = e.data.get("viewport", {})
            viewport = (float(size["width"]), float(size["height"]))
    info = {"attempt": attempt, "attempt_reason": reason, "viewport": viewport}

    train = training_samples(attempt.dots, frame_t_ms, vectors, usable, config)
    sparse = sum(1 for n in train.valid_per_dot if n < config.calib_min_frames_per_dot)
    if sparse > config.calib_max_sparse_dots:
        return _failed("too_few_valid_frames", attempts, config, valid_per_dot=train.valid_per_dot,
                       kept_per_dot=[0] * len(train.valid_per_dot), **info)  # fmt: skip

    first = select_model(train.features, train.targets, train.dot_index, config)
    if not np.isfinite(first.chosen.error_px):
        return _failed("fit_impossible", attempts, config, valid_per_dot=train.valid_per_dot,
                       kept_per_dot=[0] * len(train.valid_per_dot), **info)  # fmt: skip
    model = fit_ridge(train.features, train.targets, first.chosen.alpha, first.chosen.degree)
    keep = inlier_mask(model, train.features, train.dot_index, config)
    features, targets, dots = train.features[keep], train.targets[keep], train.dot_index[keep]
    selection = select_model(features, targets, dots, config)
    if not np.isfinite(selection.chosen.error_px):
        return _failed("fit_impossible", attempts, config, valid_per_dot=train.valid_per_dot,
                       kept_per_dot=[0] * len(train.valid_per_dot), **info)  # fmt: skip
    final = fit_ridge(features, targets, selection.chosen.alpha, selection.chosen.degree)
    kept = np.bincount(dots, minlength=len(attempt.dots)).tolist()
    return CalibrationFit("ok", None, attempt, reason, attempts, train.valid_per_dot, kept, final,
                          selection, viewport, config)  # fmt: skip


# --- The stage: files in, calibration_a.json out ---


def run_calibration(
    root: Path, study_id: int, session_id: uuid.UUID, config: PipelineConfig
) -> CalibrationFit:
    derived = derived_dir(root, study_id, session_id)
    table = pq.read_table(derived / FEATURES_FILE)
    a_ms, b = read_t_of_pts(derived / SYNC_FILE)
    t = a_ms + 1000.0 * b * table["pts"].to_numpy()
    vectors = np.stack(
        [table[c].to_numpy(zero_copy_only=False) for c in FEATURE_VECTOR_COLUMNS], axis=1
    )
    usable = np.isfinite(vectors).all(axis=1) & ~np.asarray(table["blink"].to_pylist(), dtype=bool)
    events = parse_events(events_path(root, study_id, session_id).read_bytes())
    fit = fit_calibration(events, t, vectors, usable, config)
    atomic_write_bytes(
        derived / CALIBRATION_FILE, (json.dumps(fit.as_dict(), indent=1) + "\n").encode()
    )
    return fit
