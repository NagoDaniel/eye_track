"""One session's gaze and fixations on one video, as the analysis uses them (PLAN §9).

Reads `gaze_a.parquet`, `fixations_a.parquet` and `quality.json` of the session, keeps the rows of
the video, and picks the plain or drift-corrected video coordinates (DECISIONS 2026-10-19 item 2).
Coordinates are video-normalized (0 to 1 over the content rectangle, can fall outside).
"""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from numpy.typing import NDArray

from app.events import parse_events
from app.storage import derived_dir, events_path
from worker.analysis.config import AnalysisConfig, GazeColumns
from worker.pipeline.gaze import FIXATIONS_FILE, GAZE_FILE
from worker.pipeline.quality import QUALITY_FILE
from worker.pipeline.stimulus_map import VideoRectTimeline

Floats = NDArray[np.float64]


@dataclass(frozen=True)
class Samples:
    """Valid gaze samples while the video was on screen, in time order."""

    t_ms: Floats  # client clock
    stim_time: Floats  # seconds of media time
    x: Floats
    y: Floats
    dt_s: Floats  # the time each sample stands for, in seconds


@dataclass(frozen=True)
class Fixations:
    """Fixations on the video, in time order."""

    start_t: Floats  # client clock, ms
    end_t: Floats
    stim_start: Floats  # seconds of media time
    stim_end: Floats
    duration_ms: Floats
    x: Floats  # centroid
    y: Floats


@dataclass(frozen=True)
class SessionGaze:
    session_id: uuid.UUID
    participant_code: str
    samples: Samples
    fixations: Fixations
    # Mean validation accuracy over the blocks, in px (corrected when the drift-corrected columns
    # are used); None without any.
    accuracy_px: float | None
    # Width of the video's content rectangle on this participant's screen, in px; None when unknown.
    video_width_px: float | None

    @property
    def accuracy_norm(self) -> float | None:
        """The accuracy in video-normalized units (px over the content rectangle's width)."""
        if self.accuracy_px is None or not self.video_width_px:
            return None
        return self.accuracy_px / self.video_width_px


def _floats(column: Any) -> Floats:
    return np.asarray(column.to_numpy(zero_copy_only=False), dtype=np.float64)


def accuracy_px(quality: dict[str, Any], gaze_columns: GazeColumns) -> float | None:
    key = "mean_error_corrected_px" if gaze_columns == "drift_corrected" else "mean_error_px"
    blocks = (quality.get("validation") or {}).get("blocks") or []
    values = [b[key] for b in blocks if b.get(key) is not None]
    return float(np.mean(values)) if values else None


def load_session(
    root: Path,
    study_id: int,
    session_id: uuid.UUID,
    participant_code: str,
    video_id: int,
    config: AnalysisConfig,
) -> SessionGaze:
    """Raises FileNotFoundError when a derived file is missing."""
    derived = derived_dir(root, study_id, session_id)
    gaze_columns = config.gaze_columns
    suffix = "_dc" if gaze_columns == "drift_corrected" else ""
    wanted = str(video_id)

    gaze = pq.read_table(derived / GAZE_FILE)
    on_video = np.array([v == wanted for v in gaze["video_id"].to_pylist()], dtype=np.bool_)
    valid = np.asarray(gaze["valid"].to_pylist(), dtype=np.bool_)
    t_ms, stim = _floats(gaze["t"]), _floats(gaze["stim_time"])
    x, y = _floats(gaze[f"x_vid{suffix}"]), _floats(gaze[f"y_vid{suffix}"])
    keep = on_video & valid & np.isfinite(stim) & np.isfinite(x) & np.isfinite(y)
    # Each sample stands for the time to the next webcam frame, capped (a gap is not looking).
    step = np.append(np.diff(t_ms), np.nan)
    step = np.where(np.isfinite(step), step, np.nanmedian(np.diff(t_ms)) if len(t_ms) > 1 else 0.0)
    dt_s = np.minimum(step, config.sample_max_dt_ms) / 1000.0
    samples = Samples(t_ms[keep], stim[keep], x[keep], y[keep], dt_s[keep])

    fix = pq.read_table(derived / FIXATIONS_FILE)
    fix_on_video = np.array([v == wanted for v in fix["video_id"].to_pylist()], dtype=np.bool_)
    cx, cy = _floats(fix[f"cx_vid{suffix}"]), _floats(fix[f"cy_vid{suffix}"])
    s0, s1 = _floats(fix["stim_start"]), _floats(fix["stim_end"])
    fix_keep = fix_on_video & np.isfinite(cx) & np.isfinite(cy) & np.isfinite(s0) & np.isfinite(s1)
    fixations = Fixations(
        start_t=_floats(fix["start_t"])[fix_keep],
        end_t=_floats(fix["end_t"])[fix_keep],
        stim_start=s0[fix_keep],
        stim_end=s1[fix_keep],
        duration_ms=_floats(fix["duration_ms"])[fix_keep],
        x=cx[fix_keep],
        y=cy[fix_keep],
    )

    quality = _read_quality(derived / QUALITY_FILE)
    events = parse_events(events_path(root, study_id, session_id).read_bytes())
    widths, _ = VideoRectTimeline(events).rect_at(t_ms[on_video])
    width = float(np.nanmedian(widths)) if np.isfinite(widths).any() else None
    return SessionGaze(
        session_id=session_id,
        participant_code=participant_code,
        samples=samples,
        fixations=fixations,
        accuracy_px=accuracy_px(quality, gaze_columns),
        video_width_px=width,
    )


def _read_quality(path: Path) -> dict[str, Any]:
    value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return value
