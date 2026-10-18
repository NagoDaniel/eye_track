"""Fixations by I-DT, dispersion-threshold identification (PLAN §8.9).

On the smoothed gaze of the playback phase. Samples form a run when consecutive frames all have a
smoothed position, show the same video in the same viewport. In a run, a window of at least
`fix_min_duration_ms` whose dispersion (x range plus y range) is within the threshold is grown while
it stays within it; that is a fixation. The threshold is
`max(fix_dispersion_width_fraction * viewport width, fix_precision_factor * validation precision)`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from numpy.typing import NDArray

from worker.pipeline.config import PipelineConfig

Floats = NDArray[np.floating[Any]]


def dispersion_threshold_px(
    viewport_width: float, validation_precision_px: float | None, config: PipelineConfig
) -> float:
    """The I-DT dispersion limit in px; without a measured precision only the width counts."""
    precision = validation_precision_px or 0.0
    return max(
        config.fix_dispersion_width_fraction * viewport_width,
        config.fix_precision_factor * precision,
    )


def idt(
    t_ms: Floats, x: Floats, y: Floats, dispersion_px: float, min_duration_ms: float
) -> list[tuple[int, int]]:
    """Fixations of one run of samples as inclusive index ranges `(first, last)`."""
    n = len(t_ms)
    found: list[tuple[int, int]] = []
    i = 0
    while i < n:
        j = i
        while j < n and t_ms[j] - t_ms[i] < min_duration_ms:
            j += 1
        if j >= n:
            break
        xs, ys = x[i : j + 1], y[i : j + 1]
        if np.ptp(xs) + np.ptp(ys) > dispersion_px:
            i += 1  # the window is not a fixation: drop its first sample
            continue
        x_lo, x_hi, y_lo, y_hi = xs.min(), xs.max(), ys.min(), ys.max()
        while j + 1 < n:
            nx_lo, nx_hi = min(x_lo, x[j + 1]), max(x_hi, x[j + 1])
            ny_lo, ny_hi = min(y_lo, y[j + 1]), max(y_hi, y[j + 1])
            if (nx_hi - nx_lo) + (ny_hi - ny_lo) > dispersion_px:
                break
            x_lo, x_hi, y_lo, y_hi = nx_lo, nx_hi, ny_lo, ny_hi
            j += 1
        found.append((i, j))
        i = j + 1
    return found


def runs(
    index: NDArray[np.int64],
    usable: NDArray[np.bool_],
    *keys: NDArray[Any],
) -> list[NDArray[np.int64]]:
    """Row numbers of maximal runs of usable rows whose `index` is consecutive and whose `keys`
    (video, viewport width ...) do not change."""
    rows = np.flatnonzero(usable)
    if len(rows) == 0:
        return []
    breaks = np.diff(index[rows]) != 1
    for key in keys:
        breaks |= key[rows][1:] != key[rows][:-1]
    return np.split(rows, np.flatnonzero(breaks) + 1)


@dataclass(frozen=True)
class FixationParams:
    dispersion_fraction: float
    precision_factor: float
    min_duration_ms: float
    validation_precision_px: float | None

    @staticmethod
    def of(config: PipelineConfig, precision_px: float | None) -> "FixationParams":
        return FixationParams(
            config.fix_dispersion_width_fraction,
            config.fix_precision_factor,
            config.fix_min_duration_ms,
            precision_px,
        )


def all_runs_fixations(
    run_rows: Sequence[NDArray[np.int64]],
    t_ms: Floats,
    x: Floats,
    y: Floats,
    viewport_width: Floats,
    params: FixationParams,
    config: PipelineConfig,
) -> list[NDArray[np.int64]]:
    """The fixations of every run, as arrays of row numbers."""
    out: list[NDArray[np.int64]] = []
    for rows in run_rows:
        limit = dispersion_threshold_px(
            float(viewport_width[rows[0]]), params.validation_precision_px, config
        )
        for first, last in idt(t_ms[rows], x[rows], y[rows], limit, params.min_duration_ms):
            out.append(rows[first : last + 1])
    return out
