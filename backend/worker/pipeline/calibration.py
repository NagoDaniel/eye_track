"""Calibration fit, per session (PLAN §5.5, §8.6): from the calibration dots to a gaze model.

1. The events say which dots were shown, where, and when each was clicked, in attempts (one
   calibration block each). The attempt used is the last one that passed the browser gate, else the
   last one (DECISIONS milestone 3 item 3: attempts are told apart by event order).
2. The training samples of a dot are the usable frames (a face, no blink) from 700 ms before its
   click to 100 ms after it; their target is the dot's position in viewport px. With
   `calib_fit_validation` (T14, DECISIONS 2026-11-01) the validation dots from the attempt on are
   training dots too; the sparse-dot rule of step 5 counts the attempt's own dots only.
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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pyarrow.parquet as pq
from numpy.typing import NDArray

from app.events import parse_events
from app.schemas import CalibGateResultData, EventRecord
from app.storage import atomic_write_bytes, derived_dir, events_path
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURE_VECTOR_COLUMNS
from worker.pipeline.ridge import Degree, RidgeModel, fit_ridge, predict
from worker.pipeline.spec import SPEC_A, PipelineSpec
from worker.pipeline.sync import SYNC_FILE, read_t_of_pts

CALIBRATION_FILE = SPEC_A.calibration_file
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


def validation_dots(events: Sequence[EventRecord], since_t: float) -> list[Dot]:
    """Every validation dot from `since_t` on (the chosen attempt's start), in block order."""
    return [d for block in validation_blocks(events, since_t) for d in block.dots]


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
    # The feature vector the model was fitted on (the pipeline's, PLAN §8.6).
    feature_columns: tuple[str, ...] = FEATURE_VECTOR_COLUMNS
    # T14: validation dots that were training samples too (after the attempt's own dots in
    # `valid_per_dot` and `kept_per_dot`).
    validation_dots: tuple[Dot, ...] = ()

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
            "feature_columns": list(self.feature_columns),
            "fit_validation": self.config.calib_fit_validation,
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
                    "source": "calibration" if k < len(used.dots) else "validation",
                    "valid_frames": self.valid_per_dot[k] if k < len(self.valid_per_dot) else 0,
                    "kept_frames": self.kept_per_dot[k] if k < len(self.kept_per_dot) else 0,
                }
                for k, d in enumerate([*used.dots, *self.validation_dots])
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


@dataclass(frozen=True)
class AttemptFit:
    """The fit of one calibration attempt: a model, or the reason there is none."""

    failed_reason: str | None
    valid_per_dot: list[int]
    kept_per_dot: list[int]
    model: RidgeModel | None
    selection: ModelSelection | None


def fit_attempt(
    attempt: Attempt,
    frame_t_ms: Floats,
    vectors: Floats,
    usable: NDArray[np.bool_],
    config: PipelineConfig,
    extra_dots: Sequence[Dot] = (),
) -> AttemptFit:
    """Steps 2 to 5 of the module docstring for one attempt, on its dots plus `extra_dots`
    (validation dots, T14). The sparse-dot rule looks at the attempt's own dots only, so a
    validation dot without frames never fails a calibration that passes without it."""
    dots = [*attempt.dots, *extra_dots]
    train = training_samples(dots, frame_t_ms, vectors, usable, config)
    none = [0] * len(train.valid_per_dot)
    own = train.valid_per_dot[: len(attempt.dots)]
    sparse = sum(1 for n in own if n < config.calib_min_frames_per_dot)
    if sparse > config.calib_max_sparse_dots:
        return AttemptFit("too_few_valid_frames", train.valid_per_dot, none, None, None)
    first = select_model(train.features, train.targets, train.dot_index, config)
    if not np.isfinite(first.chosen.error_px):
        return AttemptFit("fit_impossible", train.valid_per_dot, none, None, None)
    model = fit_ridge(train.features, train.targets, first.chosen.alpha, first.chosen.degree)
    keep = inlier_mask(model, train.features, train.dot_index, config)
    features, targets, dot_rows = train.features[keep], train.targets[keep], train.dot_index[keep]
    selection = select_model(features, targets, dot_rows, config)
    if not np.isfinite(selection.chosen.error_px):
        return AttemptFit("fit_impossible", train.valid_per_dot, none, None, None)
    final = fit_ridge(features, targets, selection.chosen.alpha, selection.chosen.degree)
    kept = np.bincount(dot_rows, minlength=len(dots)).tolist()
    return AttemptFit(None, train.valid_per_dot, kept, final, selection)


def viewport_before(events: Sequence[EventRecord], t_ms: float) -> tuple[float, float] | None:
    """The viewport size logged by the last `viewport_change` at or before `t_ms`."""
    viewport = None
    for e in events:
        if e.t > t_ms:
            break
        if e.type == "viewport_change":
            size = e.data.get("viewport", {})
            viewport = (float(size["width"]), float(size["height"]))
    return viewport


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
    viewport = viewport_before(events, attempt.start_t)
    extra = validation_dots(events, attempt.start_t) if config.calib_fit_validation else []
    fit = fit_attempt(attempt, frame_t_ms, vectors, usable, config, extra)
    if fit.failed_reason is not None or fit.model is None or fit.selection is None:
        return _failed(fit.failed_reason or "fit_impossible", attempts, config,
                       valid_per_dot=fit.valid_per_dot, kept_per_dot=fit.kept_per_dot,
                       attempt=attempt, attempt_reason=reason, viewport=viewport)  # fmt: skip
    return CalibrationFit(
        "ok", None, attempt, reason, attempts, fit.valid_per_dot, fit.kept_per_dot, fit.model,
        fit.selection, viewport, config, validation_dots=tuple(extra),
    )  # fmt: skip


# --- The stage: files in, calibration_a.json out ---


@dataclass(frozen=True)
class FitInputs:
    """What a calibration fit reads from a session: frame times, feature vectors, usable rows."""

    t_ms: Floats
    vectors: Floats
    usable: NDArray[np.bool_]
    events: list[EventRecord]


def fit_inputs(
    root: Path, study_id: int, session_id: uuid.UUID, spec: PipelineSpec = SPEC_A
) -> FitInputs:
    derived = derived_dir(root, study_id, session_id)
    table = pq.read_table(derived / spec.features_file)
    a_ms, b = read_t_of_pts(derived / SYNC_FILE)
    t = a_ms + 1000.0 * b * table["pts"].to_numpy()
    vectors = np.stack(
        [table[c].to_numpy(zero_copy_only=False) for c in spec.feature_columns], axis=1
    )
    usable = np.isfinite(vectors).all(axis=1) & ~np.asarray(table["blink"].to_pylist(), dtype=bool)
    events = list(parse_events(events_path(root, study_id, session_id).read_bytes()))
    return FitInputs(t, vectors, usable, events)


def run_calibration(
    root: Path,
    study_id: int,
    session_id: uuid.UUID,
    config: PipelineConfig,
    spec: PipelineSpec = SPEC_A,
) -> CalibrationFit:
    derived = derived_dir(root, study_id, session_id)
    inputs = fit_inputs(root, study_id, session_id, spec)
    t, vectors, usable, events = inputs.t_ms, inputs.vectors, inputs.usable, inputs.events
    fit = replace(fit_calibration(events, t, vectors, usable, config),
                  feature_columns=spec.feature_columns)  # fmt: skip
    atomic_write_bytes(
        derived / spec.calibration_file, (json.dumps(fit.as_dict(), indent=1) + "\n").encode()
    )
    return fit
