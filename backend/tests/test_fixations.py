"""I-DT fixations (PLAN §8.9, §13: synthetic scanpaths with known fixations)."""

from dataclasses import replace

import numpy as np
import pytest

from worker.pipeline.config import PipelineConfig
from worker.pipeline.fixations import dispersion_threshold_px, idt, runs

CONFIG = PipelineConfig()
FRAME_MS = 1000 / 30


def scanpath(
    plan: list[tuple[float, float, float]],
    saccade_ms: float = 60.0,
    noise: float = 2.0,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[tuple[float, float]]]:
    """Fixations (x, y, duration in ms) joined by straight saccades; returns t, x, y and the
    (start, end) time of each fixation."""
    rng = np.random.default_rng(seed)
    t_all, x_all, y_all, spans = [], [], [], []
    clock = 0.0
    for k, (x, y, duration) in enumerate(plan):
        n = int(duration / FRAME_MS)
        t = clock + np.arange(n) * FRAME_MS
        t_all.append(t)
        x_all.append(np.full(n, x))
        y_all.append(np.full(n, y))
        spans.append((float(t[0]), float(t[-1])))
        clock = float(t[-1]) + FRAME_MS
        if k + 1 < len(plan):
            m = max(int(saccade_ms / FRAME_MS), 1)
            fx, fy = (
                np.linspace(x, plan[k + 1][0], m + 2)[1:-1],
                np.linspace(y, plan[k + 1][1], m + 2)[1:-1],
            )
            t_all.append(clock + np.arange(m) * FRAME_MS)
            x_all.append(fx)
            y_all.append(fy)
            clock += m * FRAME_MS
    ts, xs, ys = np.concatenate(t_all), np.concatenate(x_all), np.concatenate(y_all)
    return ts, xs + rng.normal(0, noise, len(xs)), ys + rng.normal(0, noise, len(ys)), spans


def test_known_fixations_are_found_with_their_centres_and_times() -> None:
    plan = [(300.0, 300.0, 400.0), (800.0, 200.0, 600.0), (500.0, 500.0, 250.0)]
    t, x, y, spans = scanpath(plan)
    found = idt(t, x, y, dispersion_px=50.0, min_duration_ms=100.0)
    assert len(found) == 3
    for (first, last), (px, py, _), (start, end) in zip(found, plan, spans, strict=True):
        assert x[first : last + 1].mean() == pytest.approx(px, abs=3)
        assert y[first : last + 1].mean() == pytest.approx(py, abs=3)
        assert t[first] == pytest.approx(start, abs=2 * FRAME_MS)
        assert t[last] == pytest.approx(end, abs=2 * FRAME_MS)


def test_saccade_samples_are_not_in_a_fixation() -> None:
    t, x, y, _ = scanpath([(100.0, 100.0, 400.0), (900.0, 500.0, 400.0)], saccade_ms=100.0)
    found = idt(t, x, y, 50.0, 100.0)
    in_fixation = np.zeros(len(t), bool)
    for first, last in found:
        in_fixation[first : last + 1] = True
    midway = (x > 300) & (x < 700)
    assert midway.any() and not (midway & in_fixation).any()


def test_a_short_stop_is_not_a_fixation_and_the_minimum_is_a_config_value() -> None:
    # The middle stop is 3 frames: its first and last sample are 67 ms apart (the duration of a
    # fixation is measured between its first and last sample).
    t, x, y, _ = scanpath([(100.0, 100.0, 400.0), (700.0, 300.0, 100.0), (300.0, 500.0, 400.0)])
    assert len(idt(t, x, y, 50.0, CONFIG.fix_min_duration_ms)) == 2
    assert len(idt(t, x, y, 50.0, 60.0)) == 3  # a 60 ms minimum finds the stop too


def test_the_dispersion_threshold_decides_where_a_fixation_ends() -> None:
    t = np.arange(60) * FRAME_MS
    x = np.linspace(0, 120, 60)  # a slow drift of 120 px over 2 s
    y = np.zeros(60)
    assert len(idt(t, x, y, 200.0, 100.0)) == 1
    several = idt(t, x, y, 50.0, 100.0)
    assert len(several) >= 2
    for first, last in several:
        assert np.ptp(x[first : last + 1]) <= 50.0


def test_dispersion_is_the_sum_of_the_x_and_y_ranges() -> None:
    t = np.arange(10) * FRAME_MS
    x = np.array([0, 30, 0, 30, 0, 30, 0, 30, 0, 30.0])
    y = np.array([0, 30, 0, 30, 0, 30, 0, 30, 0, 30.0])
    assert idt(t, x, y, 59.0, 100.0) == []  # 30 + 30 = 60 px
    assert len(idt(t, x, y, 61.0, 100.0)) == 1


def test_too_few_samples_and_empty_input() -> None:
    assert idt(np.array([]), np.array([]), np.array([]), 50.0, 100.0) == []
    t = np.arange(3) * FRAME_MS  # 67 ms
    assert idt(t, np.zeros(3), np.zeros(3), 50.0, 100.0) == []


def test_the_threshold_is_the_larger_of_the_width_share_and_twice_the_precision() -> None:
    assert dispersion_threshold_px(1280, None, CONFIG) == pytest.approx(64.0)  # 5 % of the width
    assert dispersion_threshold_px(1280, 10.0, CONFIG) == pytest.approx(64.0)
    assert dispersion_threshold_px(1280, 50.0, CONFIG) == pytest.approx(100.0)  # 2 x precision
    loose = replace(CONFIG, fix_dispersion_width_fraction=0.1, fix_precision_factor=3.0)
    assert dispersion_threshold_px(1000, 20.0, loose) == pytest.approx(100.0)


def test_runs_break_at_missing_frames_and_changes() -> None:
    index = np.arange(12)
    usable = np.ones(12, bool)
    usable[4] = False  # a frame without a position
    video = np.array(["a"] * 8 + ["b"] * 4, dtype=object)
    width = np.array([1000.0] * 10 + [1200.0] * 2)
    split = runs(index, usable, video, width)
    assert [r.tolist() for r in split] == [[0, 1, 2, 3], [5, 6, 7], [8, 9], [10, 11]]
    assert runs(index, np.zeros(12, bool)) == []
    # Frames with a gap in the frame numbers are not consecutive even if every row is usable.
    assert [r.tolist() for r in runs(np.array([0, 1, 5, 6]), np.ones(4, bool))] == [[0, 1], [2, 3]]
