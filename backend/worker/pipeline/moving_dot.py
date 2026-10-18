"""Accuracy against the moving-dot video (PLAN §12.4, §14: the Milestone 3 pilot check).

The participant follows a dot that moves along a known path with pauses (`e2e/fixtures/
moving_dot.mp4`, ground truth in `moving_dot_path.json`). Every valid playback frame has a stimulus
time, so its gaze (in video-normalised coordinates) can be compared with where the dot was.

Errors are in viewport px and as a share of the viewport diagonal (like the validation error);
medians, because a few wild frames should not decide. The report passes when the median error is at
most `dot_max_median_error_norm` of the diagonal and at least `dot_min_valid_fraction` of the
playback frames are valid (DECISIONS milestone 3 item 9). It also gives the gaze lag (how far
behind the dot the gaze runs, which a wrong sync or an over-smoothed gaze shows) and the errors
during the pauses (fixation accuracy) and the movements separately.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
from numpy.typing import NDArray

from app.schemas import EventRecord
from worker.pipeline.config import PipelineConfig
from worker.pipeline.predict import viewport_timeline
from worker.pipeline.stimulus_map import VideoRectTimeline

Floats = NDArray[np.float64]

LAG_RANGE_MS = 400  # the gaze lag is searched from -400 to +400 ms
LAG_STEP_MS = 20


class MovingDotError(Exception):
    """The report cannot be made (no path, no playback, several videos); the message says why."""


class DotPath:
    """The ground truth: dot position in video-normalised coordinates at a stimulus time."""

    def __init__(self, data: dict[str, Any]) -> None:
        samples = data["samples"]
        self.duration_s = float(data["duration_s"])
        self._t = np.array([s["t"] for s in samples], dtype=np.float64)
        self._x = np.array([s["x"] for s in samples], dtype=np.float64) / float(data["width"])
        self._y = np.array([s["y"] for s in samples], dtype=np.float64) / float(data["height"])
        self._pauses = [(float(p["start_s"]), float(p["end_s"])) for p in data["pauses"]]

    @staticmethod
    def load(path: Path) -> "DotPath":
        return DotPath(json.loads(path.read_text(encoding="utf-8")))

    def at(self, stim_time_s: Floats) -> tuple[Floats, Floats]:
        return np.interp(stim_time_s, self._t, self._x), np.interp(stim_time_s, self._t, self._y)

    def in_pause(self, stim_time_s: Floats) -> NDArray[np.bool_]:
        out = np.zeros(len(stim_time_s), dtype=np.bool_)
        for start, end in self._pauses:
            out |= (stim_time_s >= start) & (stim_time_s <= end)
        return out


def _stats(error_px: Floats, diagonal: Floats) -> dict[str, float | int | None]:
    if len(error_px) == 0:
        return {"frames": 0, "median_px": None, "median_norm": None, "mean_px": None,
                "p90_px": None, "within_threshold": None}  # fmt: skip
    norm = error_px / diagonal
    return {
        "frames": len(error_px),
        "median_px": float(np.median(error_px)),
        "median_norm": float(np.median(norm)),
        "mean_px": float(error_px.mean()),
        "p90_px": float(np.percentile(error_px, 90)),
        "within_threshold": float((norm <= 0.15).mean()),
    }


def moving_dot_report(
    gaze: pa.Table,
    events: Sequence[EventRecord],
    path: DotPath,
    config: PipelineConfig,
    video_id: str | None = None,
) -> dict[str, Any]:
    """Compare the gaze of `gaze_a.parquet` with the dot's path. Raises `MovingDotError`."""
    column = {name: np.asarray(gaze[name].to_pylist(), dtype=object) for name in
              ("phase", "video_id", "valid")}  # fmt: skip

    def floats(name: str) -> Floats:
        return np.array(
            [np.nan if v is None else v for v in gaze[name].to_pylist()], dtype=np.float64
        )

    t, stim = floats("t"), floats("stim_time")
    playback = column["phase"] == "playback"
    videos = sorted({str(v) for v in column["video_id"][playback] if v is not None})
    if video_id is None:
        if len(videos) > 1:
            raise MovingDotError(f"the session played several videos {videos}; give the video id")
        video_id = videos[0] if videos else None
    if video_id is None or not playback.any():
        raise MovingDotError("the session has no playback frames")
    mine = playback & (column["video_id"] == video_id) & np.isfinite(stim)
    mine &= (stim >= 0) & (stim <= path.duration_s)
    frames = int(mine.sum())
    if frames == 0:
        raise MovingDotError(f"no frame of video {video_id} has a stimulus time inside the path")
    valid = column["valid"].astype(bool) & mine
    valid_fraction = float(valid.sum() / frames)

    rect_w, rect_h = VideoRectTimeline(events).rect_at(t)
    view_w, view_h = viewport_timeline(events).at(t)
    diagonal = np.hypot(view_w, view_h)

    def errors(x_name: str, y_name: str, shift_ms: float = 0.0) -> tuple[Floats, Floats, Floats]:
        """Error in px of every valid frame, with the dot position `shift_ms` earlier."""
        x, y = floats(x_name), floats(y_name)
        ok = valid & np.isfinite(x) & np.isfinite(y) & np.isfinite(rect_w)
        tx, ty = path.at(stim[ok] - shift_ms / 1000.0)
        err = np.hypot((x[ok] - tx) * rect_w[ok], (y[ok] - ty) * rect_h[ok])
        return err, diagonal[ok], stim[ok]

    result: dict[str, Any] = {
        "video_id": video_id,
        "playback_frames": frames,
        "valid_frames": int(valid.sum()),
        "valid_fraction": valid_fraction,
    }
    for label, names in (
        ("uncorrected", ("x_vid", "y_vid")),
        ("drift_corrected", ("x_vid_dc", "y_vid_dc")),
    ):
        err, diag, st = errors(*names)
        paused = path.in_pause(st)
        result[label] = {
            **_stats(err, diag),
            "pauses": _stats(err[paused], diag[paused]),
            "movements": _stats(err[~paused], diag[~paused]),
        }
    # The gaze lag: the shift of the dot's path that fits the gaze best (positive: gaze behind).
    shifts = np.arange(-LAG_RANGE_MS, LAG_RANGE_MS + 1, LAG_STEP_MS)
    medians = []
    for shift in shifts:
        err, _, _ = errors("x_vid", "y_vid", float(shift))
        medians.append(np.median(err) if len(err) else np.inf)
    best = int(np.argmin(medians))
    result["gaze_lag_ms"] = float(shifts[best]) if np.isfinite(medians[best]) else None

    median_norm = result["uncorrected"]["median_norm"]
    reasons = []
    if median_norm is None or median_norm > config.dot_max_median_error_norm:
        reasons.append("median_error_high")
    if valid_fraction < config.dot_min_valid_fraction:
        reasons.append("valid_fraction_low")
    result["criteria"] = {
        "max_median_error_norm": config.dot_max_median_error_norm,
        "min_valid_fraction": config.dot_min_valid_fraction,
    }
    result["passes"] = not reasons
    result["reasons"] = reasons
    return result


def format_report(report: dict[str, Any]) -> str:
    """The report as text for the terminal."""

    def line(label: str, stats: dict[str, Any]) -> str:
        if not stats["frames"]:
            return f"  {label:<16} no frames"
        return (
            f"  {label:<16} {stats['frames']:>5} frames  median {stats['median_px']:6.1f} px "
            f"({stats['median_norm']:.3f} of the diagonal)  p90 {stats['p90_px']:6.1f} px"
        )

    lines = [
        f"Moving-dot check, video {report['video_id']}",
        f"  playback frames {report['playback_frames']}, valid {report['valid_frames']} "
        f"({report['valid_fraction']:.0%})",
    ]
    for label in ("uncorrected", "drift_corrected"):
        lines.append(line(label, report[label]))
        lines.append(line("  in pauses", report[label]["pauses"]))
        lines.append(line("  in movements", report[label]["movements"]))
    lag = report["gaze_lag_ms"]
    lines.append(
        f"  gaze lag {'unknown' if lag is None else f'{lag:+.0f} ms'} (positive: behind the dot)"
    )
    c = report["criteria"]
    lines.append(
        f"  criteria: median error <= {c['max_median_error_norm']} of the diagonal, "
        f"valid >= {c['min_valid_fraction']:.0%}"
    )
    lines.append("PASS" if report["passes"] else f"FAIL: {', '.join(report['reasons'])}")
    return "\n".join(lines)
