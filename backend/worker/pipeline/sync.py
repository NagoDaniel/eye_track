"""Sync: map webcam frame time (pts) to the client clock `t` (PLAN §8.2 steps 1 to 4).

1. Coarse map: `t = recorder_start + 1000 * pts`.
2. Fine alignment: the browser's live face model and the server's model see the same face, so a
   per-frame signal (here two channels: the horizontal iris position in the eye-local frame, mean of
   both eyes, and the head yaw; each z-scored) is cross-correlated at 100 Hz over +-1000 ms around
   the coarse map. The channels are correlated one by one and the correlations averaged, so a
   channel that moves the other way in the two pipelines cannot cancel the other (DECISIONS
   2026-10-18, milestone 3 T5). Frames without a face and blinks are left out.
3. Drift: the same correlation in 60 s windows (the server signal moved by the whole-session lag
   first, so each window's lag is relative to it) and a Huber fit of the window lags against pts
   gives `t = a + b * pts`. The session is flagged when `|b - 1|` or a window's residual is too big.
4. `derived/sync.json` holds the map, the correlation peaks and curve, the windows and the flags.

The map from `t` to the stimulus video's time is in `stimulus_map.py`.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from numpy.typing import NDArray
from sklearn.linear_model import HuberRegressor

from app.events import parse_events, parse_records
from app.schemas import FeatureRecord
from app.storage import atomic_write_bytes, browser_features_path, derived_dir, events_path
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURES_FILE, frame_features, is_blink

SYNC_FILE = "sync.json"
SYNC_VERSION = 1

Floats = NDArray[np.float64]
Bools = NDArray[np.bool_]


class SyncError(Exception):
    """The sync inputs are unusable (not merely weak: those are flags)."""


@dataclass(frozen=True)
class WindowFit:
    pts_center: float  # seconds
    lag_ms: float  # offset from the coarse map found in this window
    peak: float | None  # correlation at that lag
    used: bool  # in the drift fit (peak high enough and not at the edge of the search)
    residual_ms: float | None  # distance from the fitted line, for the used windows


@dataclass(frozen=True)
class SyncResult:
    recorder_start_ms: float
    lag_ms: float  # whole-session offset from the coarse map
    peak: float | None  # its correlation; None when there was nothing to correlate
    a_ms: float
    b: float
    windows: list[WindowFit]
    residual_max_ms: float
    flags: list[str]
    curve_lag_ms: list[float]
    curve_r: list[float | None]
    frames: dict[str, int]
    config: dict[str, Any]

    def pts_to_t_ms(self, pts: Floats | Sequence[float]) -> Floats:
        """Client time of webcam frames: `t = a + 1000 * b * pts`."""
        return self.a_ms + 1000.0 * self.b * np.asarray(pts, dtype=np.float64)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": SYNC_VERSION,
            "config": self.config,
            "recorder_start_ms": self.recorder_start_ms,
            "lag_ms": self.lag_ms,
            "peak": self.peak,
            "t_of_pts": {"a_ms": self.a_ms, "b": self.b},
            "residual_max_ms": self.residual_max_ms,
            "windows": [
                {
                    "pts_center": w.pts_center,
                    "lag_ms": w.lag_ms,
                    "peak": w.peak,
                    "used": w.used,
                    "residual_ms": w.residual_ms,
                }
                for w in self.windows
            ],
            "curve": {"lag_ms": self.curve_lag_ms, "r": self.curve_r},
            "flags": self.flags,
            "frames": self.frames,
        }


def _zscore(values: Floats) -> Floats:
    """Per channel, over the frames that have every channel."""
    ok = np.isfinite(values).all(axis=1)
    out = np.full_like(values, np.nan)
    if ok.sum() < 2:
        return out
    mean = values[ok].mean(axis=0)
    std = values[ok].std(axis=0)
    std[std == 0] = 1.0
    out[ok] = (values[ok] - mean) / std
    return out


def _on_grid(
    times: Floats, values: Floats, grid: Floats, max_gap_ms: float
) -> tuple[Floats, Bools]:
    """Linear interpolation onto `grid`; a grid point is valid when the samples on both sides of it
    exist and are at most `max_gap_ms` apart."""
    ok = np.isfinite(values).all(axis=1)
    t, v = times[ok], values[ok]
    out = np.zeros((len(grid), values.shape[1]))
    valid = np.zeros(len(grid), dtype=np.bool_)
    if len(t) < 2:
        return out, valid
    right = np.searchsorted(t, grid)
    inside = (right > 0) & (right < len(t))
    left = np.clip(right - 1, 0, len(t) - 1)
    right = np.clip(right, 0, len(t) - 1)
    valid = inside & (t[right] - t[left] <= max_gap_ms)
    for c in range(values.shape[1]):
        out[:, c] = np.interp(grid, t, v[:, c])
    return out, valid


def _correlate(
    b: Floats, bv: Bools, s: Floats, sv: Bools, lags: NDArray[np.int64], min_overlap: int
) -> Floats:
    """Mean over channels of the Pearson correlation of `b[i]` with `s[i - k]` for each lag `k`
    (in grid samples), over the grid points where both are valid. -inf where there is too little."""
    n = len(b)
    out = np.full(len(lags), -np.inf)
    for i, k in enumerate(lags):
        k = int(k)
        if abs(k) >= n:
            continue
        if k >= 0:
            bs, ss, ok = b[k:], s[: n - k], bv[k:] & sv[: n - k]
        else:
            bs, ss, ok = b[: n + k], s[-k:], bv[: n + k] & sv[-k:]
        if ok.sum() < min_overlap:
            continue
        x, y = bs[ok], ss[ok]
        x, y = x - x.mean(axis=0), y - y.mean(axis=0)
        denominator = np.sqrt((x * x).sum(axis=0) * (y * y).sum(axis=0))
        if (denominator == 0).any():
            continue
        out[i] = float(((x * y).sum(axis=0) / denominator).mean())
    return out


def _peak(
    r: Floats, lags_ms: NDArray[np.floating[Any]], step_ms: float
) -> tuple[float, float, bool]:
    """(lag in ms, correlation, at the edge of the search) of the best lag, refined with a parabola
    through its neighbours."""
    i = int(np.argmax(r))
    if not np.isfinite(r[i]):
        return 0.0, float("nan"), True
    at_edge = i == 0 or i == len(r) - 1
    lag = float(lags_ms[i])
    if not at_edge and np.isfinite(r[i - 1]) and np.isfinite(r[i + 1]):
        curvature = r[i - 1] - 2 * r[i] + r[i + 1]
        if curvature < 0:
            lag += step_ms * 0.5 * (r[i - 1] - r[i + 1]) / curvature
    return lag, float(r[i]), at_edge


def _windows(first: float, last: float, config: PipelineConfig) -> list[tuple[float, float]]:
    """Consecutive windows of `sync_window_s` over [first, last] in pts; a short rest is merged into
    the last window. Fewer than two full windows: one window over everything."""
    length = config.sync_window_s
    if last - first < 2 * length:
        return [(first, last + 1e-9)]
    count = int((last - first) // length)
    edges = [first + i * length for i in range(count)]
    if last - edges[-1] - length >= config.sync_min_window_s:
        edges.append(edges[-1] + length)
        count += 1
    return [(edges[i], edges[i + 1] if i + 1 < len(edges) else last + 1e-9) for i in range(count)]


def estimate_sync(
    server_pts_s: Floats,
    server_values: Floats,
    browser_t_ms: Floats,
    browser_values: Floats,
    recorder_start_ms: float,
    config: PipelineConfig,
) -> SyncResult:
    """The sync of one session. Signals are (frames, channels), NaN where a frame is unusable."""
    frames = {"server": int(np.isfinite(server_values).all(axis=1).sum()),
              "browser": int(np.isfinite(browser_values).all(axis=1).sum())}  # fmt: skip
    base: dict[str, Any] = {
        "recorder_start_ms": recorder_start_ms,
        "frames": frames,
        "config": config.as_dict(),
    }
    if frames["server"] < 2 or frames["browser"] < 2:
        return SyncResult(
            lag_ms=0.0, peak=None, a_ms=recorder_start_ms, b=1.0, windows=[], residual_max_ms=0.0,
            flags=["sync_coarse_only"], curve_lag_ms=[], curve_r=[], **base,
        )  # fmt: skip

    step = 1000.0 / config.sync_rate_hz
    max_lag = int(round(config.sync_max_lag_ms / step))
    lags = np.arange(-max_lag, max_lag + 1, dtype=np.int64)
    lags_ms = lags * step
    min_overlap = max(2, int(config.sync_min_overlap_s * config.sync_rate_hz))

    s_z, b_z = _zscore(server_values), _zscore(browser_values)
    coarse = recorder_start_ms + 1000.0 * server_pts_s
    first = min(float(coarse[0]), float(browser_t_ms[0])) - config.sync_max_lag_ms
    last = max(float(coarse[-1]), float(browser_t_ms[-1])) + config.sync_max_lag_ms
    grid = np.arange(first, last, step)
    b, bv = _on_grid(browser_t_ms, b_z, grid, config.sync_max_gap_ms)

    s, sv = _on_grid(coarse, s_z, grid, config.sync_max_gap_ms)
    r = _correlate(b, bv, s, sv, lags, min_overlap)
    lag, peak, at_edge = _peak(r, lags_ms, step)
    flags: list[str] = []
    if not np.isfinite(peak) or peak < config.sync_min_peak_correlation:
        flags.append("sync_weak_peak")
    if at_edge:
        flags.append("sync_lag_at_limit")

    # Windows: the server signal moved by the whole-session lag, lags searched around that.
    s, sv = _on_grid(coarse + lag, s_z, grid, config.sync_max_gap_ms)
    pts_on_grid = (grid - recorder_start_ms - lag) / 1000.0
    windows: list[WindowFit] = []
    spans = _windows(float(server_pts_s[0]), float(server_pts_s[-1]), config)
    if len(spans) == 1:
        flags.append("sync_short")
        windows.append(WindowFit(sum(spans[0]) / 2, lag, peak, True, 0.0))
    else:
        for p0, p1 in spans:
            in_window = sv & (pts_on_grid >= p0) & (pts_on_grid < p1)
            rw = _correlate(b, bv, s, in_window, lags, min_overlap)
            extra, peak_w, edge_w = _peak(rw, lags_ms, step)
            used = bool(
                np.isfinite(peak_w) and peak_w >= config.sync_min_peak_correlation and not edge_w
            )
            windows.append(
                WindowFit(
                    (p0 + p1) / 2, lag + extra, peak_w if np.isfinite(peak_w) else None, used, None
                )
            )

    alpha, slope, residual_max = lag, 0.0, 0.0  # offset (ms), extra ms per second, worst residual
    used_windows = [w for w in windows if w.used]
    if len(spans) > 1:
        if len(used_windows) < 2:
            flags.append("sync_drift_unknown")
        else:
            x = np.array([w.pts_center for w in used_windows])
            y = np.array([w.lag_ms for w in used_windows])
            centre = float(x.mean())
            fit = HuberRegressor(epsilon=config.sync_huber_epsilon, alpha=0.0, max_iter=1000)
            fit.fit((x - centre).reshape(-1, 1), y)
            slope = float(fit.coef_[0])
            alpha = float(fit.intercept_) - slope * centre
            windows = [
                WindowFit(w.pts_center, w.lag_ms, w.peak, w.used,
                          w.lag_ms - (alpha + slope * w.pts_center) if w.used else None)
                for w in windows
            ]  # fmt: skip
            residual_max = max(abs(w.residual_ms) for w in windows if w.residual_ms is not None)
            if residual_max > config.sync_max_residual_ms:
                flags.append("sync_residual_high")
    b_slope = (1000.0 + slope) / 1000.0
    if abs(b_slope - 1.0) > config.sync_max_slope_error:
        flags.append("sync_drift_high")

    return SyncResult(
        lag_ms=lag, peak=peak if np.isfinite(peak) else None, a_ms=recorder_start_ms + alpha,
        b=b_slope, windows=windows, residual_max_ms=residual_max, flags=flags,
        curve_lag_ms=[float(v) for v in lags_ms],
        curve_r=[float(v) if np.isfinite(v) else None for v in r], **base,
    )  # fmt: skip


# --- The stage: files in, sync.json out ---

CHANNELS = 2  # horizontal iris position (mean of both eyes) and head yaw


def _server_signal(features: Path, config: PipelineConfig) -> tuple[Floats, Floats, float]:
    table = pq.read_table(features)
    metadata = table.schema.metadata or {}
    aspect = int(metadata[b"width"]) / int(metadata[b"height"])
    pts = table["pts"].to_numpy()
    iris = np.nanmean(
        [
            table["iris_l_x"].to_numpy(zero_copy_only=False),
            table["iris_r_x"].to_numpy(zero_copy_only=False),
        ],
        axis=0,
    )
    values = np.stack([iris, table["yaw"].to_numpy(zero_copy_only=False)], axis=1)
    values[np.asarray(table["blink"].to_pylist(), dtype=np.bool_)] = np.nan
    return pts, values, aspect


def _browser_signal(path: Path, aspect: float, config: PipelineConfig) -> tuple[Floats, Floats]:
    records = parse_records(path.read_bytes(), FeatureRecord, "features") if path.exists() else []
    times = np.array([r.t for r in records], dtype=np.float64)
    values = np.full((len(records), CHANNELS), np.nan)
    for i, r in enumerate(records):
        if not r.face or r.lm is None or r.matrix is None or r.blink_l is None or r.blink_r is None:
            continue
        f = frame_features(r.lm, r.matrix, aspect)
        if is_blink(r.blink_l, r.blink_r, f.open_l, f.open_r, config):
            continue
        values[i] = ((f.iris_l[0] + f.iris_r[0]) / 2, f.yaw)
    return times, values


def run_sync(
    root: Path, study_id: int, session_id: uuid.UUID, config: PipelineConfig
) -> SyncResult:
    """Read the session's features, events and browser features; write `derived/sync.json`."""
    derived = derived_dir(root, study_id, session_id)
    pts, server_values, aspect = _server_signal(derived / FEATURES_FILE, config)
    events = parse_events(events_path(root, study_id, session_id).read_bytes())
    starts = [e.t for e in events if e.type == "recorder_start"]
    if not starts:
        raise SyncError("there is no recorder_start event: the coarse offset is unknown")
    t_browser, browser_values = _browser_signal(
        browser_features_path(root, study_id, session_id), aspect, config
    )
    result = estimate_sync(pts, server_values, t_browser, browser_values, starts[0], config)
    atomic_write_bytes(
        derived / SYNC_FILE, (json.dumps(result.as_dict(), indent=1) + "\n").encode()
    )
    return result


def read_t_of_pts(path: Path) -> tuple[float, float]:
    """`(a_ms, b)` of a `sync.json`: a webcam frame is at client time `a_ms + 1000 * b * pts`."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("version") != SYNC_VERSION:
        raise SyncError(f"{path.name} has version {data.get('version')}, expected {SYNC_VERSION}")
    return float(data["t_of_pts"]["a_ms"]), float(data["t_of_pts"]["b"])
