"""Offline smoothing of the gaze (PLAN §8.7): interpolate short gaps, then an RTS smoother.

1. Invalid samples between two valid ones at most `smooth_max_interp_gap_ms` apart are filled by
   linear interpolation. They are marked `interpolated`; whether they are `valid` is not touched.
2. Runs of valid and interpolated samples separated by a longer gap are independent segments.
3. Each segment goes through a Rauch-Tung-Striebel (forward-backward Kalman) smoother with a
   constant-velocity model, x and y independently with the same noise. The process noise is large
   enough that a saccade is followed: a 300 px step settles within about 60 ms (tested).
"""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from worker.pipeline.config import PipelineConfig

Floats = NDArray[np.float64]
Bools = NDArray[np.bool_]


@dataclass(frozen=True)
class Smoothed:
    x: Floats  # smoothed px; NaN where the sample is in no segment
    y: Floats
    interpolated: Bools  # filled in by interpolation (and `valid` is false there)


def interpolate_gaps(
    t_ms: Floats, x: Floats, y: Floats, valid: Bools, max_gap_ms: float
) -> tuple[Floats, Floats, Bools]:
    """Fill runs of invalid samples whose valid neighbours are at most `max_gap_ms` apart.

    Returns new x and y (valid samples unchanged, other samples NaN unless filled) and the mask of
    the filled samples. No extrapolation before the first or after the last valid sample.
    """
    out_x = np.where(valid, x, np.nan)
    out_y = np.where(valid, y, np.nan)
    filled = np.zeros(len(t_ms), dtype=np.bool_)
    ok = np.flatnonzero(valid)
    for before, after in zip(ok[:-1], ok[1:], strict=True):
        if after - before > 1 and t_ms[after] - t_ms[before] <= max_gap_ms:
            span = slice(before + 1, after)
            fraction = (t_ms[span] - t_ms[before]) / (t_ms[after] - t_ms[before])
            out_x[span] = x[before] + (x[after] - x[before]) * fraction
            out_y[span] = y[before] + (y[after] - y[before]) * fraction
            filled[span] = True
    return out_x, out_y, filled


def _rts(t_ms: Floats, z: Floats, config: PipelineConfig) -> Floats:
    """Smooth one segment. `z` is (n, 2): the x and y measurements at times `t_ms`."""
    n = len(t_ms)
    if n == 1:
        return z.copy()
    r = config.smooth_meas_std_px**2
    q = config.smooth_process_noise
    # Filtered (and predicted) position and velocity per axis, and the covariance (pp, pv, vv),
    # which is the same for both axes.
    pos, vel = np.zeros((n, 2)), np.zeros((n, 2))
    pos_pred, vel_pred = np.zeros((n, 2)), np.zeros((n, 2))
    cov = np.zeros((n, 3))
    cov_pred = np.zeros((n, 3))
    pos[0], vel[0] = z[0], 0.0
    cov[0] = (r, 0.0, config.smooth_initial_velocity_std**2)
    pos_pred[0], vel_pred[0], cov_pred[0] = pos[0], vel[0], cov[0]
    for k in range(1, n):
        dt = max((t_ms[k] - t_ms[k - 1]) / 1000.0, 1e-6)
        pp, pv, vv = cov[k - 1]
        # predict: x' = x + v dt, v' = v; Q from white-noise acceleration with density q
        pos_pred[k] = pos[k - 1] + vel[k - 1] * dt
        vel_pred[k] = vel[k - 1]
        p_pp = pp + 2 * dt * pv + dt * dt * vv + q * dt**3 / 3
        p_pv = pv + dt * vv + q * dt**2 / 2
        p_vv = vv + q * dt
        cov_pred[k] = (p_pp, p_pv, p_vv)
        # update with the position measurement
        s = p_pp + r
        k_pos, k_vel = p_pp / s, p_pv / s
        innovation = z[k] - pos_pred[k]
        pos[k] = pos_pred[k] + k_pos * innovation
        vel[k] = vel_pred[k] + k_vel * innovation
        cov[k] = ((1 - k_pos) * p_pp, (1 - k_pos) * p_pv, p_vv - k_vel * p_pv)
    # backward pass
    smooth_pos, smooth_vel = pos.copy(), vel.copy()
    for k in range(n - 2, -1, -1):
        dt = max((t_ms[k + 1] - t_ms[k]) / 1000.0, 1e-6)
        a, b, c = cov[k]
        p, qq, rr = cov_pred[k + 1]
        det = p * rr - qq * qq
        # C = P_k F^T (P_{k+1}^-)^-1, with P_k F^T = [[a + b dt, b], [b + c dt, c]]
        row0 = (a + b * dt, b)
        row1 = (b + c * dt, c)
        c00 = (row0[0] * rr - row0[1] * qq) / det
        c01 = (-row0[0] * qq + row0[1] * p) / det
        c10 = (row1[0] * rr - row1[1] * qq) / det
        c11 = (-row1[0] * qq + row1[1] * p) / det
        d_pos = smooth_pos[k + 1] - pos_pred[k + 1]
        d_vel = smooth_vel[k + 1] - vel_pred[k + 1]
        smooth_pos[k] = pos[k] + c00 * d_pos + c01 * d_vel
        smooth_vel[k] = vel[k] + c10 * d_pos + c11 * d_vel
    return smooth_pos


def smooth_gaze(
    t_ms: Floats, x: Floats, y: Floats, valid: Bools, config: PipelineConfig
) -> Smoothed:
    """Interpolate short gaps and smooth each segment. `t_ms` must be increasing."""
    fx, fy, filled = interpolate_gaps(t_ms, x, y, valid, config.smooth_max_interp_gap_ms)
    usable = valid | filled
    out_x = np.full(len(t_ms), np.nan)
    out_y = np.full(len(t_ms), np.nan)
    rows = np.flatnonzero(usable)
    if len(rows):
        # A new segment starts where the usable samples are more than the gap limit apart.
        breaks = np.flatnonzero(np.diff(t_ms[rows]) > config.smooth_max_interp_gap_ms) + 1
        for segment in np.split(rows, breaks):
            smoothed = _rts(t_ms[segment], np.stack([fx[segment], fy[segment]], axis=1), config)
            out_x[segment], out_y[segment] = smoothed[:, 0], smoothed[:, 1]
    return Smoothed(x=out_x, y=out_y, interpolated=filled)
