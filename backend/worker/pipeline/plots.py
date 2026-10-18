"""Debug plots (PLAN §12.2): PNGs in `derived/debug/` that show what happened in a session.

- `calibration.png`: for each calibration attempt, the dots (circles) and the model's median
  prediction for each (dots), joined by a line, and the same for the validation points that followed
  (squares and triangles): how well that attempt's model fitted and how well it carried over.
- `validation.png`: the error of every validation block with and without the drift correction, and
  the drift (offset) over time with its interpolation.
- `sync.png`: the cross-correlation curve and the drift fit with the windows' residuals.
- `signals.png`: quality signals over the session: valid fraction, face size, head pose, brightness,
  webcam frame rate.
- `gaze.png`: gaze x and y over time, invalid stretches shaded.

Drawing uses matplotlib's object API (no pyplot, no global state). A plot whose inputs are missing
is skipped with the reason; one that raises is logged and recorded without stopping the others (the
session's data is fine even if a picture cannot be drawn).
"""

import io
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Patch, Rectangle
from numpy.typing import NDArray

from app.events import parse_events
from app.schemas import EventRecord
from app.storage import atomic_write_bytes, browser_features_path, derived_dir, events_path
from worker.pipeline.calibration import (
    Attempt,
    calibration_attempts,
    fit_attempt,
    training_samples,
    viewport_before,
)
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURE_VECTOR_COLUMNS, FEATURES_FILE
from worker.pipeline.gaze import FIXATIONS_FILE, GAZE_FILE, phase_intervals
from worker.pipeline.quality import QUALITY_FILE, video_spans
from worker.pipeline.ridge import predict
from worker.pipeline.sync import SYNC_FILE, read_t_of_pts
from worker.pipeline.validation import validation_blocks

log = logging.getLogger("pipeline")

DEBUG_DIR = "debug"
PLOT_FILES = ("calibration.png", "validation.png", "sync.png", "signals.png", "gaze.png")

Floats = NDArray[np.float64]
AnyFloats = NDArray[np.floating[Any]]  # for helpers that take any float array
Bools = NDArray[np.bool_]

# Colours that stay apart for the common kinds of colour blindness.
BLUE, ORANGE, GREEN, RED, GREY, PURPLE = (
    "#0072B2",
    "#E69F00",
    "#009E73",
    "#D55E00",
    "#888888",
    "#CC79A7",
)
PHASE_COLOUR = {"calibration": "#FFE8B3", "validation": "#CDEFD9", "playback": "#D6E6F5"}


class PlotSkipped(Exception):
    """The inputs of a plot are missing; the message says what."""


# --- Pure helpers ---


def spans_of(mask: Bools, t_ms: AnyFloats) -> list[tuple[float, float]]:
    """(start, end) in ms of every run of True; a run ends where the next sample begins (the last
    run lasts one median frame beyond its last sample)."""
    if len(mask) == 0:
        return []
    frame = float(np.median(np.diff(t_ms))) if len(t_ms) > 1 else 0.0
    spans: list[tuple[float, float]] = []
    i = 0
    while i < len(mask):
        if not mask[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(mask) and mask[j + 1]:
            j += 1
        end = float(t_ms[j + 1]) if j + 1 < len(mask) else float(t_ms[j]) + frame
        spans.append((float(t_ms[i]), end))
        i = j + 1
    return spans


def rolling_mean(values: AnyFloats, t_ms: AnyFloats, window_s: float) -> Floats:
    """Mean of the values within +-window/2 of each sample (NaN values ignored)."""
    half = window_s * 500.0
    lo = np.searchsorted(t_ms, t_ms - half, side="left")
    hi = np.searchsorted(t_ms, t_ms + half, side="right")
    finite = np.isfinite(values)
    sums = np.concatenate([[0.0], np.cumsum(np.where(finite, values, 0.0))])
    counts = np.concatenate([[0], np.cumsum(finite)])
    n = counts[hi] - counts[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, (sums[hi] - sums[lo]) / n, np.nan)


def rolling_rate(t_ms: AnyFloats, window_s: float) -> Floats:
    """Frames per second within +-window/2 of each frame."""
    half = window_s * 500.0
    lo = np.searchsorted(t_ms, t_ms - half, side="left")
    hi = np.searchsorted(t_ms, t_ms + half, side="right")
    span = np.minimum(t_ms + half, t_ms[-1]) - np.maximum(t_ms - half, t_ms[0])
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(span > 0, (hi - lo - 1) / (span / 1000.0), np.nan)


def png_bytes(figure: Figure) -> bytes:
    FigureCanvasAgg(figure)
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=100)
    return buffer.getvalue()


# --- Inputs ---


@dataclass
class PlotData:
    config: PipelineConfig
    events: list[EventRecord]
    features: pa.Table
    t_ms: Floats  # client time of every feature row
    sync: dict[str, Any]
    calibration: dict[str, Any]
    quality: dict[str, Any] | None
    gaze: pa.Table | None
    fixations: pa.Table | None
    brightness: tuple[Floats, Floats] | None  # (t in ms, mean luminance) from the browser
    extra: dict[str, Any] = field(default_factory=dict)

    def column(self, table: pa.Table, name: str) -> Floats:
        return np.array(
            [np.nan if v is None else v for v in table[name].to_pylist()], dtype=np.float64
        )


def _json(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def load_plot_data(
    root: Path, study_id: int, session_id: uuid.UUID, config: PipelineConfig
) -> PlotData:
    derived = derived_dir(root, study_id, session_id)
    features = pq.read_table(derived / FEATURES_FILE)
    a_ms, b = read_t_of_pts(derived / SYNC_FILE)
    t_ms = a_ms + 1000.0 * b * features["pts"].to_numpy()
    brightness = None
    browser = browser_features_path(root, study_id, session_id)
    if browser.is_file():
        rows = [
            json.loads(line) for line in browser.read_text(encoding="utf-8").splitlines() if line
        ]
        pairs = [(r["t"], r["brightness"]) for r in rows if r.get("brightness") is not None]
        if pairs:
            brightness = (np.array([p[0] for p in pairs]), np.array([p[1] for p in pairs]))
    calibration = _json(derived / "calibration_a.json")
    sync = _json(derived / SYNC_FILE)
    if calibration is None or sync is None:
        raise PlotSkipped("calibration_a.json or sync.json is missing")
    return PlotData(
        config=config,
        events=parse_events(events_path(root, study_id, session_id).read_bytes()),
        features=features,
        t_ms=t_ms,
        sync=sync,
        calibration=calibration,
        quality=_json(derived / QUALITY_FILE),
        gaze=pq.read_table(derived / GAZE_FILE) if (derived / GAZE_FILE).is_file() else None,
        fixations=pq.read_table(derived / FIXATIONS_FILE)
        if (derived / FIXATIONS_FILE).is_file()
        else None,
        brightness=brightness,
    )


def shade_phases(ax: Any, data: PlotData, origin_ms: float) -> None:
    """Background bands for the dot runs and the videos, in seconds since `origin_ms`."""
    for start, end, name in phase_intervals(data.events):
        ax.axvspan((start - origin_ms) / 1000, (end - origin_ms) / 1000, color=PHASE_COLOUR[name],
                   lw=0, zorder=0)  # fmt: skip
    for start, end in video_spans(data.events):
        stop = end if np.isfinite(end) else float(data.t_ms[-1])
        ax.axvspan((start - origin_ms) / 1000, (stop - origin_ms) / 1000,
                   color=PHASE_COLOUR["playback"], lw=0, zorder=0)  # fmt: skip


def phase_legend(ax: Any) -> None:
    ax.legend(
        handles=[Patch(color=c, label=n) for n, c in PHASE_COLOUR.items()],
        loc="upper right", fontsize=7, ncols=3, frameon=False,
    )  # fmt: skip


# --- calibration.png ---


def _median_per_dot(
    model: Any,
    dots: Sequence[Any],
    t: Floats,
    vectors: Floats,
    usable: Bools,
    config: PipelineConfig,
) -> list[tuple[float, float] | None]:
    train = training_samples(dots, t, vectors, usable, config)
    out: list[tuple[float, float] | None] = []
    if len(train.features) == 0:
        return [None] * len(dots)
    predicted = predict(model, train.features)
    for k in range(len(dots)):
        rows = train.dot_index == k
        out.append(
            (float(np.median(predicted[rows, 0])), float(np.median(predicted[rows, 1])))
            if rows.any()
            else None
        )
    return out


def plot_calibration(data: PlotData) -> Figure:
    attempts = calibration_attempts(data.events)
    if not attempts:
        raise PlotSkipped("the session has no calibration dots")
    vectors = np.stack([data.column(data.features, c) for c in FEATURE_VECTOR_COLUMNS], axis=1)
    blink = np.asarray(data.features["blink"].to_pylist(), dtype=np.bool_)
    usable = np.isfinite(vectors).all(axis=1) & ~blink
    columns = min(len(attempts), 3)
    rows = -(-len(attempts) // columns)
    figure = Figure(figsize=(5.2 * columns, 3.4 * rows), layout="constrained")
    axes = figure.subplots(rows, columns, squeeze=False)
    for k, attempt in enumerate(attempts):
        ax = axes[k // columns][k % columns]
        _calibration_panel(ax, data, attempt, attempts, vectors, usable)
    for k in range(len(attempts), rows * columns):
        axes[k // columns][k % columns].axis("off")
    figure.suptitle("Calibration attempts: targets and the model's median predictions", fontsize=11)
    return figure


def _calibration_panel(
    ax: Any,
    data: PlotData,
    attempt: Attempt,
    attempts: list[Attempt],
    vectors: Floats,
    usable: Bools,
) -> None:
    viewport = viewport_before(data.events, attempt.start_t) or (1280.0, 720.0)
    shown: list[tuple[float, float]] = []  # every point drawn, to size the axes
    ax.add_patch(Rectangle((0, 0), viewport[0], viewport[1], fill=False, ec=GREY, lw=0.8))
    gate = (
        "no browser gate"
        if attempt.gate is None
        else ("gate passed" if attempt.passed else "gate failed")
    )
    fit = fit_attempt(attempt, data.t_ms, vectors, usable, data.config)
    if fit.model is None or fit.selection is None:
        ax.set_title(f"Attempt {attempt.number} ({gate}): no fit, {fit.failed_reason}", fontsize=8)
        return
    best = fit.selection.chosen
    ax.set_title(
        f"Attempt {attempt.number} ({gate}): {'quadratic' if best.degree == 2 else 'linear'}, "
        f"CV {best.error_px:.0f} px",
        fontsize=8,
    )
    medians = _median_per_dot(fit.model, attempt.dots, data.t_ms, vectors, usable, data.config)
    for dot, median in zip(attempt.dots, medians, strict=True):
        ax.plot(dot.x, dot.y, "o", mfc="none", mec=BLUE, ms=7, label=None)
        shown.append((dot.x, dot.y))
        if median is not None:
            ax.plot([dot.x, median[0]], [dot.y, median[1]], "-", color=BLUE, lw=0.8, alpha=0.7)
            ax.plot(median[0], median[1], ".", color=BLUE, ms=5)
            shown.append(median)
    later = [a.start_t for a in attempts if a.start_t > attempt.start_t]
    for block in validation_blocks(data.events, attempt.start_t):
        if later and block.start_t >= later[0]:
            break
        points = _median_per_dot(fit.model, block.dots, data.t_ms, vectors, usable, data.config)
        for dot, median in zip(block.dots, points, strict=True):
            ax.plot(dot.x, dot.y, "s", mfc="none", mec=ORANGE, ms=7)
            shown.append((dot.x, dot.y))
            if median is not None:
                ax.plot([dot.x, median[0]], [dot.y, median[1]], "-", color=ORANGE, lw=0.8)
                ax.plot(median[0], median[1], "^", color=ORANGE, ms=5)
                shown.append(median)
        break  # the block that followed this attempt
    _fit_axes(ax, shown, viewport)
    ax.tick_params(labelsize=6)


OFF_SCREEN_LIMIT = 0.6  # the axes reach at most this share of the viewport beyond its edges


def _fit_axes(ax: Any, points: list[tuple[float, float]], viewport: tuple[float, float]) -> None:
    """Axes that hold the screen and the points, up to a limit; the rest is counted in the label."""
    w, h = viewport
    xs, ys = np.array([p[0] for p in points]), np.array([p[1] for p in points])
    x_lo = max(min(0.0, float(xs.min())), -OFF_SCREEN_LIMIT * w) - 0.05 * w
    x_hi = min(max(w, float(xs.max())), (1 + OFF_SCREEN_LIMIT) * w) + 0.05 * w
    y_lo = max(min(0.0, float(ys.min())), -OFF_SCREEN_LIMIT * h) - 0.05 * h
    y_hi = min(max(h, float(ys.max())), (1 + OFF_SCREEN_LIMIT) * h) + 0.05 * h
    ax.set_xlim(x_lo, x_hi)
    ax.set_ylim(y_hi, y_lo)  # y grows downwards, like the screen
    ax.set_aspect("equal")
    off = int(((xs < x_lo) | (xs > x_hi) | (ys < y_lo) | (ys > y_hi)).sum())
    if off:
        ax.set_xlabel(f"{off} predictions are further away than the plot shows", fontsize=7)


# --- validation.png ---


def plot_validation(data: PlotData) -> Figure:
    validation = None if data.quality is None else data.quality.get("validation")
    if not validation or not validation.get("blocks"):
        raise PlotSkipped(
            "there is no validation result (quality stage not run, or no calibration)"
        )
    blocks = [b for b in validation["blocks"] if b["t_ms"] is not None]
    if not blocks:
        raise PlotSkipped("no validation block had a valid frame")
    origin = float(data.t_ms[0])
    figure = Figure(figsize=(9, 6), layout="constrained")
    top, bottom = figure.subplots(2, 1, sharex=True)
    for block in blocks:
        x = (block["t_ms"] - origin) / 1000
        pts = [p for p in block["points"] if p["error_norm"] is not None]
        top.plot([x] * len(pts), [p["error_norm"] for p in pts], ".", color=BLUE, alpha=0.4)
        top.plot(
            [x] * len(pts), [p["error_corrected_norm"] for p in pts], ".", color=GREEN, alpha=0.4
        )
    top.plot([(b["t_ms"] - origin) / 1000 for b in blocks], [b["mean_error_norm"] for b in blocks],
             "-o", color=BLUE, label="mean error")  # fmt: skip
    top.plot([(b["t_ms"] - origin) / 1000 for b in blocks],
             [b["mean_error_corrected_norm"] for b in blocks], "-o", color=GREEN,
             label="mean error after drift correction")  # fmt: skip
    top.set_ylabel("error / viewport diagonal")
    top.set_title("Validation error per block (dots: single points)", fontsize=10)
    top.legend(fontsize=8)
    drift = validation.get("drift")
    if drift:
        t = (np.array(drift["t_ms"]) - origin) / 1000
        bottom.plot(t, drift["dx_px"], "o", color=ORANGE)
        bottom.plot(t, drift["dy_px"], "o", color=PURPLE)
        grid = np.linspace(0, (data.t_ms[-1] - origin) / 1000, 200)
        bottom.plot(grid, np.interp(grid, t, drift["dx_px"]), "-", color=ORANGE, label="x offset")
        bottom.plot(grid, np.interp(grid, t, drift["dy_px"]), "-", color=PURPLE, label="y offset")
        bottom.legend(fontsize=8)
    bottom.axhline(0, color=GREY, lw=0.6)
    bottom.set_ylabel("offset: prediction - target (px)")
    bottom.set_xlabel("session time (s)")
    bottom.set_title("Drift: the offset at each block and the line used to correct it", fontsize=10)
    return figure


# --- sync.png ---


def plot_sync(data: PlotData) -> Figure:
    sync = data.sync
    curve_lag = np.array(sync["curve"]["lag_ms"], dtype=np.float64)
    curve_r = np.array([np.nan if r is None else r for r in sync["curve"]["r"]], dtype=np.float64)
    figure = Figure(figsize=(11, 5), layout="constrained")
    left, right, residual = figure.subplots(1, 3, gridspec_kw={"width_ratios": [1.2, 1.5, 1]})
    if len(curve_lag):
        left.plot(curve_lag, curve_r, color=BLUE)
        left.axvline(sync["lag_ms"], color=RED, lw=1, label=f"lag {sync['lag_ms']:.0f} ms")
        left.legend(
            fontsize=8, title=f"peak {sync['peak']:.2f}" if sync["peak"] is not None else None
        )
    else:
        left.text(
            0.5, 0.5, "no correlation\n(coarse offset only)", ha="center", transform=left.transAxes
        )
    left.set_xlabel("lag from the coarse offset (ms)")
    left.set_ylabel("correlation")
    left.set_title("Cross-correlation, whole session", fontsize=10)

    windows = sync["windows"]
    a_ms, b = sync["t_of_pts"]["a_ms"], sync["t_of_pts"]["b"]
    start = sync["recorder_start_ms"]
    if len(windows) <= 1:
        message = (
            "no drift fit: the session is shorter than\ntwo windows, so one offset and slope 1"
            if windows
            else "no windows (coarse offset only)"
        )
        right.text(
            0.5, 0.5, message, ha="center", va="center", transform=right.transAxes, fontsize=9
        )
        residual.text(0.5, 0.5, "n/a", ha="center", va="center", transform=residual.transAxes)
        for ax in (right, residual):
            ax.set_xticks([])
            ax.set_yticks([])
    else:
        x = np.array([w["pts_center"] for w in windows])
        y = np.array([w["lag_ms"] for w in windows])
        used = np.array([w["used"] for w in windows])
        right.plot(x[used], y[used], "o", color=BLUE, label="used in the fit")
        right.plot(x[~used], y[~used], "x", color=RED, label="left out")
        right.set_ylim(min(float(y.min()), -50.0) - 10, max(float(y.max()), 50.0) + 10)
        grid = np.linspace(0, max(float(x.max()) * 1.05, 1.0), 50)
        right.plot(grid, a_ms - start + 1000 * (b - 1) * grid, "-", color=ORANGE,
                   label=f"fit: slope {1000 * (b - 1):+.2f} ms/s")  # fmt: skip
        right.legend(fontsize=8)
        res = [(w["pts_center"], w["residual_ms"]) for w in windows if w["residual_ms"] is not None]
        if res:
            residual.bar(
                [r[0] for r in res], [r[1] for r in res], width=max(x.max() * 0.04, 1), color=BLUE
            )
        limit = data.config.sync_max_residual_ms
        residual.axhline(limit, color=RED, lw=0.8, ls="--")
        residual.axhline(-limit, color=RED, lw=0.8, ls="--")
    right.set_xlabel("webcam time (s)")
    right.set_ylabel("lag (ms)")
    right.set_title("Lag per window and the fitted line", fontsize=10)
    residual.set_xlabel("webcam time (s)")
    residual.set_ylabel("residual (ms)")
    residual.set_title(f"Residuals (limit {data.config.sync_max_residual_ms:.0f} ms)", fontsize=10)
    flags = ", ".join(sync["flags"]) or "no flags"
    figure.suptitle(f"Sync: {flags}", fontsize=11)
    return figure


# --- signals.png ---


def plot_signals(data: PlotData) -> Figure:
    t = data.t_ms
    origin = float(t[0])
    x = (t - origin) / 1000
    face = np.asarray(data.features["face"].to_pylist(), dtype=np.bool_)
    figure = Figure(figsize=(11, 10), layout="constrained")
    axes = figure.subplots(5, 1, sharex=True)
    for ax in axes:
        shade_phases(ax, data, origin)
    valid_series: Floats
    label = "face found"
    valid_series = face.astype(np.float64)
    if data.gaze is not None:
        valid_series = np.asarray(data.gaze["valid"].to_pylist(), dtype=np.float64)
        label = "valid gaze"
    axes[0].plot(x, rolling_mean(valid_series, t, 5.0), color=BLUE)
    axes[0].set_ylim(-0.02, 1.02)
    axes[0].set_ylabel(f"{label}\n(5 s mean)")
    ratio = data.column(data.features, "face_ratio")
    axes[1].plot(x, ratio, color=GREEN, lw=0.8)
    if data.quality and data.quality.get("face_ratio"):
        reference = data.quality["face_ratio"]
        for level in (reference["min_warn"], reference["max_warn"]):
            axes[1].axhline(level * reference["calibration_median"], color=RED, lw=0.8, ls="--")
    axes[1].set_ylabel("face size\n(eye distance / width)")
    for name, colour in (("yaw", BLUE), ("pitch", ORANGE), ("roll", PURPLE)):
        axes[2].plot(x, data.column(data.features, name), color=colour, lw=0.8, label=name)
    axes[2].legend(fontsize=7, ncols=3, loc="upper left", frameon=False)
    axes[2].set_ylabel("head angle (deg)")
    if data.brightness is not None:
        bt, bv = data.brightness
        axes[3].plot((bt - origin) / 1000, bv, color=ORANGE, lw=0.8)
        axes[3].set_ylim(0, 1)
    else:
        axes[3].text(
            0.5,
            0.5,
            "no brightness in the browser features",
            ha="center",
            transform=axes[3].transAxes,
        )
    axes[3].set_ylabel("brightness\n(browser)")
    axes[4].plot(x, rolling_rate(t, 2.0), color=PURPLE, lw=0.8)
    axes[4].set_ylim(bottom=0)
    axes[4].set_ylabel("webcam frames/s\n(2 s window)")
    axes[4].set_xlabel("session time (s)")
    phase_legend(axes[0])
    figure.suptitle("Quality signals over the session", fontsize=11)
    return figure


# --- gaze.png ---


def plot_gaze(data: PlotData) -> Figure:
    if data.gaze is None:
        raise PlotSkipped("there is no gaze (the calibration failed or the gaze stage was not run)")
    g = data.gaze
    t = data.column(g, "t")
    origin = float(t[0])
    x = (t - origin) / 1000
    valid = np.asarray(g["valid"].to_pylist(), dtype=np.bool_)
    interpolated = np.asarray(g["interpolated"].to_pylist(), dtype=np.bool_)
    viewport = (1280.0, 720.0)
    if data.quality is not None:
        viewport = viewport_before(data.events, float(t[-1])) or viewport
    figure = Figure(figsize=(11, 7), layout="constrained")
    axes = figure.subplots(2, 1, sharex=True)
    for ax, raw_name, sm_name, size, label in (
        (axes[0], "x_px", "x_sm", viewport[0], "gaze x (px)"),
        (axes[1], "y_px", "y_sm", viewport[1], "gaze y (px)"),
    ):
        shade_phases(ax, data, origin)
        for start, end in spans_of(~valid & ~interpolated, t):
            ax.axvspan((start - origin) / 1000, (end - origin) / 1000, color=GREY, alpha=0.35, lw=0)
        for start, end in spans_of(interpolated, t):
            ax.axvspan(
                (start - origin) / 1000, (end - origin) / 1000, color=PURPLE, alpha=0.3, lw=0
            )
        ax.plot(x, data.column(g, raw_name), ".", color=ORANGE, ms=2, alpha=0.6, label="raw")
        ax.plot(x, data.column(g, sm_name), "-", color=BLUE, lw=1, label="smoothed")
        ax.axhline(0, color=GREY, lw=0.6)
        ax.axhline(size, color=GREY, lw=0.6)
        # The screen and a margin: wild raw predictions would squash everything else.
        ax.set_ylim((1.5 * size, -0.5 * size) if sm_name == "y_sm" else (-0.5 * size, 1.5 * size))
        ax.set_ylabel(label)
    if data.fixations is not None and data.fixations.num_rows:
        starts, ends = data.column(data.fixations, "start_t"), data.column(data.fixations, "end_t")
        for s, e in zip(starts, ends, strict=True):
            axes[1].plot([(s - origin) / 1000, (e - origin) / 1000], [viewport[1] * 1.04] * 2, "-",
                         color=GREEN, lw=3, solid_capstyle="butt")  # fmt: skip
    axes[1].set_xlabel("session time (s)")
    axes[0].legend(fontsize=8, loc="upper left", ncols=2, frameon=False)
    phase_legend(axes[0])
    figure.suptitle(
        "Gaze over time (grey: invalid, purple: interpolated, green bars: fixations; "
        "predictions beyond half a screen outside it are not shown)",
        fontsize=10,
    )
    return figure


# --- moving-dot path vs gaze (for the pilot check, not a stage) ---


def plot_moving_dot(gaze: pa.Table, path: Any, report: dict[str, Any]) -> Figure:
    """Gaze (video-normalised) against the dot's path over the stimulus time."""
    stim = np.array([np.nan if v is None else v for v in gaze["stim_time"].to_pylist()])
    video = np.asarray(gaze["video_id"].to_pylist(), dtype=object)
    valid = np.asarray(gaze["valid"].to_pylist(), dtype=np.bool_)
    ok = np.isfinite(stim) & (video == report["video_id"]) & valid
    figure = Figure(figsize=(11, 6), layout="constrained")
    axes = figure.subplots(2, 1, sharex=True)
    grid = np.linspace(0, path.duration_s, 600)
    truth = path.at(grid)
    for ax, i, name in ((axes[0], 0, "x_vid"), (axes[1], 1, "y_vid")):
        y = np.array([np.nan if v is None else v for v in gaze[name].to_pylist()])
        ax.plot(grid, truth[i], "-", color=GREY, lw=2, label="dot")
        ax.plot(stim[ok], y[ok], ".", color=BLUE, ms=3, label="gaze")
        ax.set_ylabel(f"{name[0]} (video width/height)")
    axes[1].invert_yaxis()
    axes[1].set_xlabel("video time (s)")
    axes[0].legend(fontsize=8)
    verdict = "PASS" if report["passes"] else "FAIL: " + ", ".join(report["reasons"])
    figure.suptitle(
        f"Moving dot: median error {report['uncorrected']['median_px']:.0f} px, "
        f"{report['valid_fraction']:.0%} valid, {verdict}", fontsize=11,
    )  # fmt: skip
    return figure


# --- The stage ---

PLOTS: dict[str, Callable[[PlotData], Figure]] = {
    "calibration.png": plot_calibration,
    "validation.png": plot_validation,
    "sync.png": plot_sync,
    "signals.png": plot_signals,
    "gaze.png": plot_gaze,
}


@dataclass(frozen=True)
class PlotsSummary:
    written: list[str]
    skipped: dict[str, str]
    errors: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {"written": self.written, "skipped": self.skipped, "errors": self.errors}


def run_plots(
    root: Path, study_id: int, session_id: uuid.UUID, config: PipelineConfig
) -> PlotsSummary:
    """Draw every plot whose inputs exist into `derived/debug/`. A plot that cannot be drawn is
    recorded (and its old picture removed), never raised: the data is fine without the picture."""
    directory = derived_dir(root, study_id, session_id) / DEBUG_DIR
    written: list[str] = []
    skipped: dict[str, str] = {}
    errors: dict[str, str] = {}
    try:
        data = load_plot_data(root, study_id, session_id, config)
    except PlotSkipped as exc:
        for name in PLOTS:
            (directory / name).unlink(missing_ok=True)
        return PlotsSummary([], {name: str(exc) for name in PLOTS}, {})
    for name, draw in PLOTS.items():
        try:
            atomic_write_bytes(directory / name, png_bytes(draw(data)))
            written.append(name)
        except PlotSkipped as exc:
            (directory / name).unlink(missing_ok=True)
            skipped[name] = str(exc)
        except Exception as exc:
            (directory / name).unlink(missing_ok=True)
            errors[name] = f"{type(exc).__name__}: {exc}"
            log.exception("plot failed", extra={"plot": name})
    return PlotsSummary(written, skipped, errors)
