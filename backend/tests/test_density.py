"""The density core of the heatmaps (PLAN §9.1, §13)."""

import uuid

import numpy as np

from worker.analysis.config import AnalysisConfig
from worker.analysis.density import (
    by_stim_time,
    choose_sigma,
    grid_axes,
    group_density,
    participant_density,
)
from worker.analysis.load import Fixations, Samples, SessionGaze

CONFIG = AnalysisConfig()
CELL = 1 / (CONFIG.grid_width * CONFIG.grid_height)  # area of a cell, video-normalized


def xy(*points: tuple[float, float]) -> tuple[np.ndarray, np.ndarray]:
    a = np.array(points, dtype=np.float64).reshape(-1, 2)
    return a[:, 0], a[:, 1]


def peak(grid: np.ndarray) -> tuple[float, float]:
    """Video-normalized position of the highest cell."""
    j, i = np.unravel_index(int(np.argmax(grid)), grid.shape)
    cx, cy = grid_axes(CONFIG)
    return float(cx[i]), float(cy[j])


def gaze(accuracy_px: float | None, width_px: float | None) -> SessionGaze:
    empty = np.zeros(0)
    return SessionGaze(
        session_id=uuid.uuid4(),
        participant_code="P",
        samples=Samples(empty, empty, empty, empty, empty),
        fixations=Fixations(empty, empty, empty, empty, empty, empty, empty),
        accuracy_px=accuracy_px,
        video_width_px=width_px,
    )


def test_one_sample_gives_a_gaussian_at_that_place() -> None:
    x, y = xy((0.3, 0.7))
    grid = participant_density(x, y, 0.05, CONFIG)
    assert grid.shape == (36, 64)
    px, py = peak(grid)
    assert abs(px - 0.3) <= 0.5 / 64 and abs(py - 0.7) <= 0.5 / 36


def test_the_shape_is_a_gaussian_of_the_given_sigma() -> None:
    x, y = xy((0.5, 0.5))
    sigma = 0.1
    grid = participant_density(x, y, sigma, CONFIG)
    cx, cy = grid_axes(CONFIG)
    expected = np.exp(-((cx[None, :] - 0.5) ** 2 + (cy[:, None] - 0.5) ** 2) / (2 * sigma**2))
    np.testing.assert_allclose(grid, expected, rtol=1e-12)
    # the integral of a unit Gaussian over the plane is 2 pi sigma^2
    assert abs(grid.sum() * CELL - 2 * np.pi * sigma**2) < 0.02 * 2 * np.pi * sigma**2


def test_a_participant_weighs_the_same_whatever_the_sample_count() -> None:
    many = xy(*[(0.4, 0.5)] * 100)
    few = xy(*[(0.4, 0.5)] * 10)
    a = participant_density(*many, 0.05, CONFIG)
    b = participant_density(*few, 0.05, CONFIG)
    np.testing.assert_allclose(a, b)
    both = group_density([many, few], 0.05, CONFIG)
    np.testing.assert_allclose(both, 2 * a)


def test_spread_samples_have_the_same_mass_as_concentrated_ones() -> None:
    spread = xy(*[(0.3 + 0.01 * k, 0.5) for k in range(20)])
    still = xy(*[(0.4, 0.5)] * 3)
    mass_spread = participant_density(*spread, 0.05, CONFIG).sum()
    mass_still = participant_density(*still, 0.05, CONFIG).sum()
    assert abs(mass_spread - mass_still) / mass_still < 0.01


def test_participants_without_samples_add_nothing() -> None:
    nobody = (np.zeros(0), np.zeros(0))
    x, y = xy((0.5, 0.5))
    np.testing.assert_allclose(
        group_density([nobody, (x, y)], 0.05, CONFIG), participant_density(x, y, 0.05, CONFIG)
    )
    assert group_density([nobody], 0.05, CONFIG).sum() == 0
    assert group_density([], 0.05, CONFIG).shape == (36, 64)


def test_two_places_two_peaks() -> None:
    grid = group_density([xy((0.2, 0.2)), xy((0.8, 0.8))], 0.04, CONFIG)
    left, right = grid.copy(), grid.copy()
    left[:, 32:] = 0
    right[:, :32] = 0
    assert abs(peak(left)[0] - 0.2) < 0.02 and abs(peak(left)[1] - 0.2) < 0.03
    assert abs(peak(right)[0] - 0.8) < 0.02 and abs(peak(right)[1] - 0.8) < 0.03


def test_sigma_is_the_median_accuracy_in_video_units() -> None:
    sessions = [gaze(100, 1000), gaze(160, 1000), gaze(400, 1000), gaze(None, 1000), gaze(50, None)]
    sigma = choose_sigma(sessions, CONFIG)
    assert sigma.value == np.median([0.1, 0.16, 0.4]) and sigma.sessions_used == 3
    assert not sigma.floored


def test_sigma_has_a_floor() -> None:
    sigma = choose_sigma([gaze(10, 1000), gaze(20, 1000)], CONFIG)
    assert sigma.value == CONFIG.sigma_min and sigma.floored
    none = choose_sigma([gaze(None, 1000)], CONFIG)
    assert none.value == CONFIG.sigma_min and none.sessions_used == 0 and none.floored
    assert choose_sigma([], CONFIG).value == 0.03
    assert choose_sigma([gaze(100, 1000)], AnalysisConfig(sigma_min=0.2)).value == 0.2


def test_samples_are_sliced_by_media_time_whatever_their_order() -> None:
    stim = np.array([3.0, 1.0, 2.0, 5.0, 4.0])
    s = Samples(np.arange(5.0), stim, stim / 10, np.zeros(5), np.zeros(5))
    ordered = by_stim_time(s)
    x, _ = ordered.window(2.0, 4.0)  # both ends included
    np.testing.assert_allclose(x, [0.2, 0.3, 0.4])
    assert len(ordered.window(5.1, 9)[0]) == 0
    assert len(ordered.window(0, 0.5)[0]) == 0
