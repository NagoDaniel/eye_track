"""Thresholds of the processing pipeline (PLAN §1: configuration over constants).

Every number the plan names for a stage is a field here with the plan's default; a stage takes the
config as an argument and writes the values it used into its output (DECISIONS, milestone 3 item
10). Starting values the plan leaves to tuning are marked as such. Not editable per study.
"""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PipelineConfig:
    # --- Pipeline A features (PLAN §8.4) ---
    # blink = eyeBlink blendshape above this in either eye ...
    blink_score_threshold: float = 0.5
    # ... or an eye openness (lid distance / eye width) below this. Starting value, tune on
    # calibration data: in the face clip blinks measure 0.07 to 0.14 and open eyes 0.27 to 0.39.
    blink_openness_threshold: float = 0.15

    # --- Sync (PLAN §8.2) ---
    # Both pipelines' signals are put on this grid for the cross-correlation.
    sync_rate_hz: float = 100.0
    # Lag search around the coarse offset (and, per window, around the whole-session offset).
    sync_max_lag_ms: float = 1000.0
    # Length of the windows of the drift fit.
    sync_window_s: float = 60.0
    # A last window shorter than this is merged into the one before it. Not in the plan.
    sync_min_window_s: float = 30.0
    # The session is flagged when the fitted slope differs from 1 by more than this ...
    sync_max_slope_error: float = 0.001
    # ... or a window's offset is further than this from the fit.
    sync_max_residual_ms: float = 40.0
    # Correlation below this flags the session; it never blocks (DECISIONS milestone 3 item 11).
    sync_min_peak_correlation: float = 0.3
    # Missing features (no face, a blink) are bridged by interpolation up to this long. Not in the
    # plan; the same limit as the interpolation of the gaze in §8.7.
    sync_max_gap_ms: float = 150.0
    # Fewest common samples (at the grid rate) for a correlation to count, in seconds.
    sync_min_overlap_s: float = 1.0
    # Huber threshold of the drift fit in units of the robust scale (scikit-learn's default).
    sync_huber_epsilon: float = 1.35

    # --- Stimulus time (PLAN §8.2 step 5) ---
    # Two stimulus frames count as a gap (pause, stall) when the wall clock advanced this much more
    # than the media time between them. Dropped frames do not (both advance together). Not in the
    # plan; display refresh jitter in the recorded session is under 10 ms.
    stim_gap_tolerance_ms: float = 50.0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
