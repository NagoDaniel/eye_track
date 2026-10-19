"""The gaze density of a group over the video (PLAN §9.1).

Each participant's samples in a time window become a sum of Gaussians on a grid over the video
content rectangle (video-normalized coordinates, 0 to 1 on both axes); each participant has the same
weight however many samples they have (a sample weighs 1 / n), and the group density is the sum over
participants. The Gaussian is isotropic in video-normalized units (DECISIONS 2026-10-19 item 3), so
on screen it is wider than tall by the video's aspect ratio.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from worker.analysis.config import AnalysisConfig
from worker.analysis.load import Samples, SessionGaze

Floats = NDArray[np.float64]


@dataclass(frozen=True)
class Sigma:
    value: float  # video-normalized units
    sessions_used: int  # sessions that had a validation accuracy
    floored: bool  # the median was below the lower limit

    def as_dict(self) -> dict[str, float | int | bool]:
        return {"value": self.value, "sessions_used": self.sessions_used, "floored": self.floored}


def choose_sigma(sessions: Sequence[SessionGaze], config: AnalysisConfig) -> Sigma:
    """The median validation accuracy of the sessions in video-normalized units, at least
    `config.sigma_min`. Sessions without an accuracy are skipped; with none, the lower limit."""
    accuracies = [a for s in sessions if (a := s.accuracy_norm) is not None]
    median = float(np.median(accuracies)) if accuracies else 0.0
    return Sigma(max(median, config.sigma_min), len(accuracies), median < config.sigma_min)


@dataclass(frozen=True)
class StimSamples:
    """A participant's samples ordered by media time, so a time window is a slice."""

    stim_time: Floats
    x: Floats
    y: Floats

    def window(self, lo: float, hi: float) -> tuple[Floats, Floats]:
        """Samples with media time in `[lo, hi]`."""
        a = int(np.searchsorted(self.stim_time, lo, side="left"))
        b = int(np.searchsorted(self.stim_time, hi, side="right"))
        return self.x[a:b], self.y[a:b]


def by_stim_time(samples: Samples) -> StimSamples:
    order = np.argsort(samples.stim_time, kind="stable")
    return StimSamples(samples.stim_time[order], samples.x[order], samples.y[order])


def grid_axes(config: AnalysisConfig) -> tuple[Floats, Floats]:
    """Cell centres along x and y, in video-normalized units."""
    return (
        (np.arange(config.grid_width) + 0.5) / config.grid_width,
        (np.arange(config.grid_height) + 0.5) / config.grid_height,
    )


def participant_density(x: Floats, y: Floats, sigma: float, config: AnalysisConfig) -> Floats:
    """Mean of the Gaussians at the samples, on the grid, shape `(grid_height, grid_width)`.
    Zeros without samples. The Gaussian is separable, so this is one matrix product."""
    cx, cy = grid_axes(config)
    if len(x) == 0:
        return np.zeros((config.grid_height, config.grid_width))
    gx = np.exp(-((cx[None, :] - x[:, None]) ** 2) / (2 * sigma**2))
    gy = np.exp(-((cy[None, :] - y[:, None]) ** 2) / (2 * sigma**2))
    return np.asarray((gy.T @ gx) / len(x))


def group_density(
    participants: Sequence[tuple[Floats, Floats]], sigma: float, config: AnalysisConfig
) -> Floats:
    """The sum over participants of their density; each `(x, y)` is one participant's samples."""
    total = np.zeros((config.grid_height, config.grid_width))
    for x, y in participants:
        total += participant_density(x, y, sigma, config)
    return total
