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

    # --- Calibration fit (PLAN §5.5, §8.6) ---
    # The training window of a dot, relative to its click (excludes the saccade at the dot's onset).
    calib_window_before_click_ms: float = 700.0
    calib_window_after_click_ms: float = 100.0
    # Regularisation grid of the leave-one-dot-out cross-validation: log grid 1e-3 to 1e3.
    calib_alphas: tuple[float, ...] = (1e-3, 1e-2, 1e-1, 1.0, 1e1, 1e2, 1e3)
    # Samples whose prediction is further than this many MADs from their dot's median prediction
    # are dropped and the model is fitted again. The MAD is the median distance to that median.
    calib_outlier_mad: float = 2.5
    # The calibration failed when more than `calib_max_sparse_dots` dots have fewer than
    # `calib_min_frames_per_dot` valid frames.
    calib_min_frames_per_dot: int = 5
    calib_max_sparse_dots: int = 3

    # --- Pipeline B features (PLAN §8.5, DECISIONS 2026-10-20) ---
    # The face crop is a square around the centre of the landmark box, with the longer side of the
    # box times this (the plan's "expanded 20 %"), resized to the network's input size.
    b_crop_scale: float = 1.2
    # Faces per batch through the network.
    b_batch_size: int = 8

    # --- Prediction and smoothing (PLAN §8.7) ---
    # A prediction is valid inside the viewport expanded by this share of its size on every side.
    valid_viewport_margin: float = 0.15
    # Invalid samples are interpolated when their valid neighbours are at most this far apart; a
    # longer gap splits the smoother into segments.
    smooth_max_interp_gap_ms: float = 150.0
    # Constant-velocity Kalman model, x and y alike: the position measurement noise (px) and the
    # white-noise acceleration density (px^2 / s^3). Tuned on synthetic data (PLAN §8.7, DECISIONS
    # 2026-10-18 T7): with 15 px of measurement noise a 300 px step is within 30 px of its new level
    # 17 ms after it at 30 fps (60 ms asked), and this q is where fixation noise is reduced most.
    smooth_meas_std_px: float = 15.0
    smooth_process_noise: float = 3.0e7
    # Prior spread of the velocity at the start of a segment (px/s): effectively unknown.
    smooth_initial_velocity_std: float = 2000.0

    # --- Fixations, I-DT (PLAN §8.9) ---
    # Dispersion threshold = max(fraction * viewport width, factor * session validation precision).
    fix_dispersion_width_fraction: float = 0.05
    fix_precision_factor: float = 2.0
    # Shortest fixation.
    fix_min_duration_ms: float = 100.0

    # --- Quality report (PLAN §8.10) ---
    # A stretch without a face counts as a face-lost episode from this long on (the browser's
    # debounce, PLAN §5.7).
    quality_face_lost_min_ms: float = 500.0
    # A gap between two webcam frames longer than this is counted as a gap in the recording.
    quality_frame_gap_ms: float = 100.0

    # --- Moving-dot acceptance report (PLAN §12.4, §14; DECISIONS milestone 3 item 9) ---
    # The pilot passes when the median error against the dot's path is at most this share of the
    # viewport diagonal and at least this share of the playback frames is valid.
    dot_max_median_error_norm: float = 0.15
    dot_min_valid_fraction: float = 0.8

    # --- Debug replay video (PLAN §12.2) ---
    replay_fps: float = 30.0
    # The video is scaled to this width (up or down); the timeline bar is added below it.
    replay_width: int = 960

    # --- Stimulus time (PLAN §8.2 step 5) ---
    # Two stimulus frames count as a gap (pause, stall) when the wall clock advanced this much more
    # than the media time between them. Dropped frames do not (both advance together). Not in the
    # plan; display refresh jitter in the recorded session is under 10 ms.
    stim_gap_tolerance_ms: float = 50.0

    def as_dict(self) -> dict[str, Any]:
        """Plain JSON types (tuples as lists), for the files that record the values used."""
        return {k: list(v) if isinstance(v, tuple) else v for k, v in asdict(self).items()}
