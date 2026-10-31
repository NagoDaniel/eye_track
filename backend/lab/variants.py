"""Variants of Pipeline A that the product cannot express yet, applied inside the lab only.

- **Validation dots in the fit** (E7) and **the drift correction** (E6): see their sections below.

- **Feature columns**: the regression is fitted on any columns of `features_a.parquet` (e.g. without
  `face_ratio`, or with the head translation `tx, ty, tz`), through a `PipelineSpec` with other
  `feature_columns`; the product's stages take the spec as an argument, so no product change.
- **High-pass features** (`<column>_hp`): the column minus its own running baseline over
  `window_s` seconds (centred, over usable frames: a face, no blink). The baseline is a trimmed
  mean (`HP_TRIM_FRACTION` cut at each end): robust to half-closed lids the blink rule misses, and,
  unlike a median, it stays between the levels of a signal that alternates between two values
  (looking up, then down), where a median jumps to whichever level is more frequent.
  The idea (E1): lid openness, face size and head angles carry gaze information that changes
  within seconds, and posture or fatigue drift that changes over tens of seconds; the running
  baseline removes the slow part.
  Caveat that the moving-dot test cannot show: when a participant looks at one region of a real
  video for longer than about half the window, the high-pass also removes part of the true gaze
  signal there and pulls the estimate towards the centre.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from numpy.typing import NDArray

from app.events import parse_events
from app.storage import atomic_write_bytes, derived_dir, events_path
from lab.layout import SessionRef
from worker.pipeline.calibration import (
    CalibrationFit,
    calibration_attempts,
    choose_attempt,
    fit_attempt,
    viewport_before,
)
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURE_COLUMNS, FEATURE_VECTOR_COLUMNS
from worker.pipeline.spec import PipelineSpec
from worker.pipeline.stimulus_map import VideoRectTimeline
from worker.pipeline.sync import SYNC_FILE, read_t_of_pts
from worker.pipeline.validation import BlockScore, validate_session, validation_blocks

Floats = NDArray[np.float64]
HP_SUFFIX = "_hp"
HP_TRIM_FRACTION = 0.1  # cut from each end of a window before averaging
# Columns that are features (not blink scores, which only decide validity).
BASE_COLUMNS = tuple(c for c in FEATURE_COLUMNS if not c.startswith("blink"))


@dataclass(frozen=True)
class Highpass:
    columns: tuple[str, ...]  # base columns to high-pass
    window_s: float


@dataclass(frozen=True)
class Variant:
    """What an experiment changes besides the PipelineConfig."""

    feature_columns: tuple[str, ...] = FEATURE_VECTOR_COLUMNS
    highpass: Highpass | None = None
    fit_validation: str = "none"  # E7: also fit on validation dots (FIT_VALIDATION)
    drift: str = "offset"  # E6: drift correction (DRIFT_MODES); "offset" is the product's
    blink_margin_ms: float = 0.0  # item 1: frames this close to a blink count as blinks too
    blink_relative_delta: float | None = None  # item 1: blink when score > session median + this
    model: str = "product"  # items 2, 3, 6, 7 (lab/models.py MODELS)
    x_features: tuple[str, ...] = ()  # ridge_xy: the columns for x ...
    y_features: tuple[str, ...] = ()  # ... and for y
    extra_training: str = "none"  # item 5 (lab/models.py EXTRA_TRAINING)

    def available(self) -> tuple[str, ...]:
        extra = () if self.highpass is None else tuple(c + HP_SUFFIX for c in self.highpass.columns)
        return BASE_COLUMNS + extra


def running_baseline(
    t_s: Floats, x: Floats, use: NDArray[np.bool_], window_s: float, trim: float = HP_TRIM_FRACTION
) -> Floats:
    """Trimmed mean of `x[use]` within `window_s / 2` of each time (NaN where the window has
    none). `t_s` must be increasing."""
    ts, xs = t_s[use & np.isfinite(x)], x[use & np.isfinite(x)]
    half = window_s / 2
    lo = np.searchsorted(ts, t_s - half, side="left")
    hi = np.searchsorted(ts, t_s + half, side="right")
    out = np.full(len(t_s), np.nan)
    for k in range(len(t_s)):
        n = hi[k] - lo[k]
        if n > 0:
            cut = int(n * trim)
            window = np.sort(xs[lo[k] : hi[k]])
            out[k] = float(window[cut : n - cut].mean())
    return out


def highpassed(table: pa.Table, hp: Highpass) -> dict[str, Floats]:
    t = table["pts"].to_numpy().astype(np.float64)
    blink = np.asarray(table["blink"].to_pylist(), dtype=bool)
    face = np.asarray(table["face"].to_pylist(), dtype=bool)
    out = {}
    for c in hp.columns:
        x = np.array([np.nan if v is None else v for v in table[c].to_pylist()], dtype=np.float64)
        out[c + HP_SUFFIX] = x - running_baseline(t, x, face & ~blink, hp.window_s)
    return out


def add_highpass(path: Path, hp: Highpass) -> None:
    """Append the `<column>_hp` columns to a features table, in place (metadata kept)."""
    table = pq.read_table(path)
    metadata = table.schema.metadata
    for name, values in highpassed(table, hp).items():
        table = table.append_column(name, pa.array(values, pa.float64(), mask=np.isnan(values)))
    pq.write_table(table.replace_schema_metadata(metadata), path, compression="snappy")


def check_variant(v: Variant) -> list[str]:
    """Problems with a variant (empty when it is fine)."""
    problems = []
    if not v.feature_columns:
        problems.append("features must name at least one column")
    unknown = [c for c in v.feature_columns if c not in v.available()]
    if unknown:
        problems.append(f"unknown feature columns {unknown} (available: {list(v.available())})")
    if len(set(v.feature_columns)) != len(v.feature_columns):
        problems.append("a feature column is named twice")
    if v.fit_validation not in FIT_VALIDATION:
        problems.append(f"fit_validation must be one of {list(FIT_VALIDATION)}")
    if v.drift not in DRIFT_MODES:
        problems.append(f"drift must be one of {DRIFT_MODES}")
    from lab.models import EXTRA_TRAINING, MODELS

    if v.model not in MODELS:
        problems.append(f"model must be one of {MODELS}")
    if v.extra_training not in EXTRA_TRAINING:
        problems.append(f"extra_training must be one of {EXTRA_TRAINING}")
    if v.model == "product" and v.extra_training != "none":
        problems.append('extra_training needs a lab model (model = "ridge" or another)')
    if v.model != "product" and v.drift != "offset":
        problems.append('drift = "affine" works with model = "product" only')
    if (v.model == "ridge_xy") != bool(v.x_features and v.y_features):
        problems.append('x_features and y_features go with model = "ridge_xy", and it needs both')
    bad = [c for c in (*v.x_features, *v.y_features) if c not in v.available()]
    if bad:
        problems.append(f"unknown x_features or y_features columns {bad}")
    if v.blink_margin_ms < 0:
        problems.append("blink_margin_ms must be 0 or more")
    if v.blink_relative_delta is not None and not v.blink_relative_delta > 0:
        problems.append("blink_relative_delta must be positive")
    if v.highpass is not None:
        bad = [c for c in v.highpass.columns if c not in BASE_COLUMNS]
        if bad or not v.highpass.columns:
            problems.append(f"highpass.columns must be some of {list(BASE_COLUMNS)}")
        if not v.highpass.window_s > 0:
            problems.append("highpass.window_s must be positive")
    return problems


def replace_with_highpass(columns: Sequence[str], hp_columns: Sequence[str]) -> tuple[str, ...]:
    """`columns` with each of `hp_columns` replaced by its high-passed version."""
    return tuple(c + HP_SUFFIX if c in hp_columns else c for c in columns)


# --- E7: validation dots in the calibration fit ---------------------------------------------------

# Which validation blocks join the calibration fit, by the block's kind. `before_and_between`
# leaves out the block after the last video: with two videos the second one is then scored on a
# model that saw nothing recorded after it started (the honest check of T14).
FIT_VALIDATION: dict[str, tuple[str, ...]] = {
    "none": (),
    "before_video": ("before_video",),
    "before_and_between": ("before_video", "between_videos"),
    "all": ("before_video", "between_videos", "after_last_video"),
}


def refit_with_validation(
    root: Path, ref: SessionRef, config: PipelineConfig, spec: PipelineSpec, which: str
) -> str | None:
    """Fit the calibration again on the chosen attempt's dots plus the validation dots of the
    blocks named by `which` (`before_video`: only the blocks before the first video; `all`), with
    the product's own fit (`fit_attempt`: leave-one-dot-out CV over all dots, outlier removal).
    Rewrites the calibration file; returns the reason when the fit fails, else None. The validation
    error of such a session is no longer a held-out measure; the moving dot still is."""
    derived = derived_dir(root, ref.study_id, ref.session_id)
    table = pq.read_table(derived / spec.features_file)
    a_ms, b = read_t_of_pts(derived / SYNC_FILE)
    t = a_ms + 1000.0 * b * table["pts"].to_numpy()
    vectors = np.stack(
        [table[c].to_numpy(zero_copy_only=False) for c in spec.feature_columns], axis=1
    )
    usable = np.isfinite(vectors).all(axis=1) & ~np.asarray(table["blink"].to_pylist(), dtype=bool)
    events = parse_events(events_path(root, ref.study_id, ref.session_id).read_bytes())
    attempts = calibration_attempts(events)
    chosen = choose_attempt(attempts)
    if chosen is None:
        return "no_calibration_dots"
    attempt, reason = chosen
    blocks = validation_blocks(events, attempt.start_t)
    kinds = FIT_VALIDATION[which]
    extra = [d for blk in blocks if blk.kind in kinds for d in blk.dots]
    combined = replace(attempt, dots=[*attempt.dots, *extra])
    fit = fit_attempt(combined, t, vectors, usable, config)
    if fit.model is None or fit.selection is None:
        return fit.failed_reason or "fit_impossible"
    result = CalibrationFit(
        "ok", None, combined, reason, attempts, fit.valid_per_dot, fit.kept_per_dot, fit.model,
        fit.selection, viewport_before(events, attempt.start_t), config, spec.feature_columns,
    )  # fmt: skip
    atomic_write_bytes(
        derived / spec.calibration_file, (json.dumps(result.as_dict(), indent=1) + "\n").encode()
    )
    return None


# --- E6: the drift correction ------------------------------------------------------------------

DRIFT_MODES = ("offset", "affine")


@dataclass(frozen=True)
class BlockMap:
    """target = gain * prediction + shift, per axis, fitted on one validation block."""

    t_ms: float
    gain: tuple[float, float]
    shift: tuple[float, float]

    def apply(self, x: Floats, y: Floats) -> tuple[Floats, Floats]:
        return self.gain[0] * x + self.shift[0], self.gain[1] * y + self.shift[1]


def block_map(block: BlockScore, mode: str) -> BlockMap | None:
    """`offset`: gain 1, shift = minus the block's mean offset (the product's drift). `affine`:
    least squares per axis of the target on the median prediction of the block's points (falls back
    to `offset` on an axis whose predictions do not vary)."""
    points = [p for p in block.points if p.median_x is not None and p.median_y is not None]
    if block.t_ms is None or not points:
        return None
    pred = np.array([[p.median_x, p.median_y] for p in points], dtype=np.float64)
    target = np.array([[p.x, p.y] for p in points], dtype=np.float64)
    gain, shift = [1.0, 1.0], list(np.mean(target - pred, axis=0))
    if mode == "affine":
        for axis in (0, 1):
            if len(points) >= 3 and np.ptp(pred[:, axis]) > 0:
                g, s = np.polyfit(pred[:, axis], target[:, axis], 1)
                gain[axis], shift[axis] = float(g), float(s)
    return BlockMap(block.t_ms, (gain[0], gain[1]), (float(shift[0]), float(shift[1])))


def correct(maps: Sequence[BlockMap], t_ms: Floats, x: Floats, y: Floats) -> tuple[Floats, Floats]:
    """Each block's map applied, blended linearly in time between neighbouring blocks (the nearest
    block's map before the first and after the last), as the product interpolates its offsets."""
    ordered = sorted(maps, key=lambda m: m.t_ms)
    times = np.array([m.t_ms for m in ordered])
    outs = [m.apply(x, y) for m in ordered]
    k = np.clip(np.searchsorted(times, t_ms, side="right") - 1, 0, len(ordered) - 1)
    k1 = np.clip(k + 1, 0, len(ordered) - 1)
    span = times[k1] - times[k]
    with np.errstate(invalid="ignore", divide="ignore"):
        w = np.where(span > 0, np.clip((t_ms - times[k]) / span, 0, 1), 0.0)
    xs, ys = np.stack([o[0] for o in outs]), np.stack([o[1] for o in outs])
    rows = np.arange(len(t_ms))
    return (
        (1 - w) * xs[k, rows] + w * xs[k1, rows],
        (1 - w) * ys[k, rows] + w * ys[k1, rows],
    )


def rewrite_drift(
    root: Path, ref: SessionRef, config: PipelineConfig, spec: PipelineSpec, mode: str
) -> None:
    """Recompute the drift-corrected columns of the gaze table with `mode` (the fixation table's
    corrected centroids are left as they are; the lab does not score them)."""
    derived = derived_dir(root, ref.study_id, ref.session_id)
    validation = validate_session(root, ref.study_id, ref.session_id, config, spec=spec)
    maps = [m for blk in validation.blocks if (m := block_map(blk, mode)) is not None]
    path = derived / spec.gaze_file
    table = pq.read_table(path)

    def col(name: str) -> Floats:
        return np.array([np.nan if v is None else v for v in table[name].to_pylist()])

    t = col("t")
    nan = np.full(len(t), np.nan)
    if maps:
        x_px_dc, y_px_dc = correct(maps, t, col("x_px"), col("y_px"))
        x_sm_dc, y_sm_dc = correct(maps, t, col("x_sm"), col("y_sm"))
        events = parse_events(events_path(root, ref.study_id, ref.session_id).read_bytes())
        x_vid_dc, y_vid_dc = VideoRectTimeline(events).normalise(t, x_sm_dc, y_sm_dc)
    else:
        x_px_dc = y_px_dc = x_sm_dc = y_sm_dc = x_vid_dc = y_vid_dc = nan
    metadata = table.schema.metadata
    for name, values in (
        ("x_px_dc", x_px_dc), ("y_px_dc", y_px_dc), ("x_sm_dc", x_sm_dc), ("y_sm_dc", y_sm_dc),
        ("x_vid_dc", x_vid_dc), ("y_vid_dc", y_vid_dc),
    ):  # fmt: skip
        i = table.schema.get_field_index(name)
        table = table.set_column(
            i, name, pa.array(values.astype(np.float32), pa.float32(), mask=np.isnan(values))
        )
    pq.write_table(table.replace_schema_metadata(metadata), path, compression="snappy")


# --- Item 1: blink handling --------------------------------------------------------------------


def widened_blinks(
    t_s: Floats, blink: NDArray[np.bool_], score: Floats, margin_s: float, delta: float | None
) -> NDArray[np.bool_]:
    """`blink`, plus frames within `margin_s` of a blink frame, plus (with `delta`) frames whose
    eyeBlink score is above the session's median score plus `delta`."""
    out = blink.copy()
    if delta is not None:
        finite = np.isfinite(score)
        if finite.any():
            out |= finite & (score > float(np.median(score[finite])) + delta)
    if margin_s > 0 and out.any():
        times = t_s[out]
        lo = np.searchsorted(times, t_s - margin_s, side="left")
        hi = np.searchsorted(times, t_s + margin_s, side="right")
        out = out | (hi > lo)
    return out


def widen_blinks(path: Path, margin_ms: float, delta: float | None) -> None:
    """Rewrite the `blink` column of a features table (metadata kept)."""
    table = pq.read_table(path)
    metadata = table.schema.metadata

    def col(name: str) -> Floats:
        return np.array([np.nan if v is None else v for v in table[name].to_pylist()])

    blink = np.asarray(table["blink"].to_pylist(), dtype=bool)
    score = np.fmax(col("blink_l"), col("blink_r"))
    new = widened_blinks(table["pts"].to_numpy().astype(np.float64), blink, score,
                         margin_ms / 1000.0, delta)  # fmt: skip
    table = table.set_column(table.schema.get_field_index("blink"), "blink", pa.array(new))
    pq.write_table(table.replace_schema_metadata(metadata), path, compression="snappy")
