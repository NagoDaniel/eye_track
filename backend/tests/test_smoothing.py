"""Interpolation and the RTS smoother (PLAN §8.7, §13: step response on synthetic data)."""

from dataclasses import replace

import numpy as np
import pytest

from worker.pipeline.config import PipelineConfig
from worker.pipeline.smoothing import interpolate_gaps, smooth_gaze

CONFIG = PipelineConfig()
STEP_PX = 300.0
NOISE_PX = 15.0


def grid(n: int, fps: float = 30.0) -> np.ndarray:
    return np.arange(n) * 1000.0 / fps


def step_signal(
    fps: float, noise: float, seed: int = 0
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float]:
    """A 10 s trace with a 300 px step in x half way between two samples."""
    rng = np.random.default_rng(seed)
    n = int(fps * 10)
    t = grid(n, fps)
    t_step = t[n // 2] - 500.0 / fps
    truth = np.where(t >= t_step, STEP_PX, 0.0)
    return t, truth, truth + rng.normal(0, noise, n), t_step


@pytest.mark.parametrize("fps", [30.0, 60.0, 100.0])
def test_a_300_px_step_settles_within_60_ms(fps: float) -> None:
    """Within 30 px (10 %) of the new level at every sample 60 ms after the step, and the same
    before it: the smoother (which sees the future) must not smear a saccade."""
    t, truth, x, t_step = step_signal(fps, noise=0.0)
    out = smooth_gaze(t, x, np.zeros_like(x), np.ones(len(t), bool), CONFIG).x
    late = t >= t_step + 60
    early = t <= t_step - 60
    assert np.abs(out[late] - STEP_PX).max() <= 0.1 * STEP_PX
    assert np.abs(out[early]).max() <= 0.1 * STEP_PX


@pytest.mark.parametrize("fps", [30.0, 100.0])
def test_the_step_with_measurement_noise_still_settles(fps: float) -> None:
    """With 15 px of noise the average response over many runs is within 30 px of the new level
    from 60 ms after the step (a single run also carries the noise that remains after smoothing)."""
    runs = []
    for seed in range(40):
        t, _, x, t_step = step_signal(fps, NOISE_PX, seed)
        runs.append(smooth_gaze(t, x, np.zeros_like(x), np.ones(len(t), bool), CONFIG).x)
    mean = np.mean(runs, axis=0)
    assert np.abs(mean[t >= t_step + 60] - STEP_PX).max() <= 0.1 * STEP_PX
    assert np.abs(mean[t <= t_step - 60]).max() <= 0.1 * STEP_PX
    # ... and a single run's remaining noise is well below the measurement noise.
    single = np.array(runs)[:, t >= t_step + 60] - STEP_PX
    assert np.sqrt(np.mean(single**2)) < 0.8 * NOISE_PX


def test_fixation_noise_is_reduced() -> None:
    rng = np.random.default_rng(1)
    t = grid(600)
    x = 500 + rng.normal(0, NOISE_PX, len(t))
    out = smooth_gaze(t, x, x * 0, np.ones(len(t), bool), CONFIG).x
    assert np.sqrt(np.mean((out - 500) ** 2)) < 0.8 * NOISE_PX


def test_motion_at_constant_speed_is_followed_without_lag() -> None:
    t = grid(200)
    x = 100 + 0.5 * t  # 500 px/s
    out = smooth_gaze(t, x, x * 0 + 50, np.ones(len(t), bool), CONFIG)
    assert np.abs(out.x - x).max() < 1.0 and np.abs(out.y - 50).max() < 1.0


def test_the_process_noise_is_a_config_value() -> None:
    t, truth, x, t_step = step_signal(100.0, noise=0.0)
    heavy = replace(CONFIG, smooth_process_noise=1e3)
    out = smooth_gaze(t, x, x * 0, np.ones(len(t), bool), heavy).x
    assert np.abs(out[t >= t_step + 60] - STEP_PX).max() > 0.1 * STEP_PX  # smeared over seconds


def test_x_and_y_are_smoothed_independently() -> None:
    rng = np.random.default_rng(2)
    t = grid(300)
    x = rng.normal(0, 20, len(t)) + 400
    y = np.where(t > 5000, 300.0, 100.0)
    both = smooth_gaze(t, x, y, np.ones(len(t), bool), CONFIG)
    alone = smooth_gaze(t, x, y * 0, np.ones(len(t), bool), CONFIG)
    np.testing.assert_allclose(both.x, alone.x)


# --- gaps -----------------------------------------------------------------------------------------


def trace_with_gap(invalid: list[int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    t = grid(40)
    x = 10.0 * np.arange(40)
    valid = np.ones(40, bool)
    valid[invalid] = False
    return t, x, valid


def test_gaps_of_150_ms_or_less_are_interpolated_and_marked() -> None:
    t, x, valid = trace_with_gap([10, 11, 12])  # neighbours 133 ms apart
    fx, fy, filled = interpolate_gaps(t, x, x + 1000, valid, CONFIG.smooth_max_interp_gap_ms)
    assert filled.tolist() == [i in (10, 11, 12) for i in range(40)]
    np.testing.assert_allclose(fx[10:13], x[10:13])  # the trace is a line: interpolation is exact
    np.testing.assert_allclose(fy[10:13], x[10:13] + 1000)
    assert not valid[10]  # interpolation never makes a sample valid


def test_longer_gaps_are_left_alone() -> None:
    t, x, valid = trace_with_gap([10, 11, 12, 13])  # neighbours 167 ms apart
    fx, _, filled = interpolate_gaps(t, x, x, valid, CONFIG.smooth_max_interp_gap_ms)
    assert not filled.any() and np.isnan(fx[10:14]).all()


def test_no_extrapolation_at_the_ends() -> None:
    t, x, valid = trace_with_gap([0, 1, 38, 39])
    fx, _, filled = interpolate_gaps(t, x, x, valid, CONFIG.smooth_max_interp_gap_ms)
    assert not filled.any() and np.isnan(fx[[0, 1, 38, 39]]).all()


def test_the_gap_limit_is_a_config_value() -> None:
    t, x, valid = trace_with_gap([10, 11, 12, 13])
    wide = replace(CONFIG, smooth_max_interp_gap_ms=200.0)
    assert interpolate_gaps(t, x, x, valid, wide.smooth_max_interp_gap_ms)[2][10:14].all()


def test_invalid_samples_get_no_smoothed_value_and_interpolated_ones_do() -> None:
    t, x, valid = trace_with_gap([10, 11, 12, 20, 21, 22, 23])
    out = smooth_gaze(t, x, x, valid, CONFIG)
    assert np.isfinite(out.x[10:13]).all() and out.interpolated[10:13].all()
    assert np.isnan(out.x[20:24]).all() and not out.interpolated[20:24].any()
    assert np.isfinite(out.x[:20]).all() and np.isfinite(out.x[24:]).all()


def test_a_long_gap_splits_the_smoother_into_independent_segments() -> None:
    rng = np.random.default_rng(3)
    t = grid(120)
    x = rng.normal(0, 10, 120)
    valid = np.ones(120, bool)
    valid[50:60] = False  # 10 frames, 333 ms
    together = smooth_gaze(t, x, x, valid, CONFIG)
    changed = x.copy()
    changed[60:] += 500.0  # the second segment moves far away
    moved = smooth_gaze(t, changed, changed, valid, CONFIG)
    np.testing.assert_allclose(together.x[:50], moved.x[:50])  # first segment unaffected
    first_only = smooth_gaze(t[:50], x[:50], x[:50], valid[:50], CONFIG)
    np.testing.assert_allclose(together.x[:50], first_only.x)


def test_a_segment_of_one_sample_is_returned_as_it_is() -> None:
    t = grid(30)
    x = np.full(30, 100.0)
    valid = np.zeros(30, bool)
    valid[[0, 15]] = True  # two isolated samples
    out = smooth_gaze(t, x + np.arange(30), x, valid, CONFIG)
    assert out.x[0] == 100.0 and out.x[15] == 115.0 and np.isnan(out.x[1])


def test_nothing_valid_gives_nothing() -> None:
    t = grid(10)
    out = smooth_gaze(t, np.zeros(10), np.zeros(10), np.zeros(10, bool), CONFIG)
    assert np.isnan(out.x).all() and not out.interpolated.any()


def test_irregular_frame_times_are_handled() -> None:
    rng = np.random.default_rng(4)
    t = np.cumsum(rng.uniform(20, 50, 300))
    x = 0.2 * t
    out = smooth_gaze(t, x, x, np.ones(300, bool), CONFIG)
    assert np.abs(out.x - x).max() < 1.0  # constant velocity regardless of the spacing
