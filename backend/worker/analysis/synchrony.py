"""Attentional synchrony over the video (PLAN §9.4).

For each window of `synchrony_window_ms` (200 ms), every participant with samples in it has a median
gaze position. **Dispersion** is the mean distance of those medians to their coordinate-wise group
median, in units of the video's width (the vertical distance is scaled by height over width, so
distances are the same in every direction on screen). **NSS** (normalized scanpath saliency) of a
participant is the mean, over their samples in the window, of the leave-one-out group density
(the others' density of §9.1, same sigma) z-scored over the grid: it is high where the others look
too and negative where nobody else does. A sample outside the video counts at the nearest edge cell.
A window needs at least two participants with samples; otherwise its values are empty.
"""

import csv
import io
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from numpy.typing import NDArray  # noqa: E402

from app.storage import atomic_write_bytes  # noqa: E402
from worker.analysis.config import AnalysisConfig  # noqa: E402
from worker.analysis.density import StimSamples, participant_density  # noqa: E402

SYNCHRONY_FILE = "synchrony.csv"
NSS_FILE = "synchrony_nss.csv"
SYNCHRONY_PLOT = "synchrony.png"
MIN_PARTICIPANTS = 2
Floats = NDArray[np.float64]


@dataclass(frozen=True)
class Synchrony:
    window_start_s: Floats
    window_s: float
    participants: list[str]
    count: NDArray[np.int64]  # participants with samples in each window
    dispersion: Floats  # NaN where fewer than two participants
    nss: Floats  # (windows, participants); NaN where undefined

    @property
    def nss_mean(self) -> Floats:
        """The mean NSS of each window over the participants that have one."""
        with np.errstate(all="ignore"):
            valid = np.isfinite(self.nss)
            total = np.where(valid, self.nss, 0.0).sum(axis=1)
            return np.where(valid.any(axis=1), total / np.maximum(valid.sum(axis=1), 1), np.nan)

    def summary(self) -> dict[str, object]:
        def mean(values: Floats) -> float | None:
            finite = values[np.isfinite(values)]
            return float(finite.mean()) if finite.size else None

        return {
            "windows": len(self.window_start_s),
            "windows_with_two_or_more": int((self.count >= MIN_PARTICIPANTS).sum()),
            "mean_dispersion": mean(self.dispersion),
            "mean_nss": mean(self.nss_mean),
            "nss_by_participant": {
                code: mean(self.nss[:, i]) for i, code in enumerate(self.participants)
            },
        }


def _distance_to_group_median(medians: Floats, aspect: float) -> float:
    centre = np.median(medians, axis=0)
    dx = medians[:, 0] - centre[0]
    dy = (medians[:, 1] - centre[1]) / aspect
    return float(np.hypot(dx, dy).mean())


def synchrony(
    participants: Sequence[tuple[str, StimSamples]],
    duration_s: float,
    aspect: float,
    sigma: float,
    config: AnalysisConfig,
) -> Synchrony:
    """`aspect` is the video's width over its height."""
    window = config.synchrony_window_ms / 1000.0
    windows = max(1, math.ceil(duration_s / window - 1e-9))
    starts = np.asarray(np.arange(windows) * window, dtype=np.float64)
    n = len(participants)
    count = np.zeros(windows, dtype=np.int64)
    dispersion = np.full(windows, np.nan)
    nss = np.full((windows, n), np.nan)
    for w, start in enumerate(starts):
        end = float(np.nextafter(start + window, -np.inf))  # [start, start + window)
        cut = [p.window(float(start), end) for _, p in participants]
        have = [i for i, (x, _) in enumerate(cut) if len(x) > 0]
        count[w] = len(have)
        if len(have) < MIN_PARTICIPANTS:
            continue
        medians = np.array([[np.median(cut[i][0]), np.median(cut[i][1])] for i in have])
        dispersion[w] = _distance_to_group_median(medians, aspect)
        grids = {i: participant_density(cut[i][0], cut[i][1], sigma, config) for i in have}
        total = sum(grids.values())
        for i in have:
            others = total - grids[i]
            spread = others.std()
            if spread <= 1e-12:
                continue
            z = (others - others.mean()) / spread
            col = np.clip((cut[i][0] * config.grid_width).astype(int), 0, config.grid_width - 1)
            row = np.clip((cut[i][1] * config.grid_height).astype(int), 0, config.grid_height - 1)
            nss[w, i] = float(z[row, col].mean())
    return Synchrony(starts, window, [code for code, _ in participants], count, dispersion, nss)


def _cell(value: float) -> str:
    return "" if not math.isfinite(value) else repr(round(float(value), 6))


def write_synchrony(run_dir: Path, result: Synchrony) -> list[str]:
    """`synchrony.csv` (a row per window), `synchrony_nss.csv` (a row per window and participant)
    and `synchrony.png` (dispersion and mean NSS over the video). Returns the file names."""
    series = io.StringIO()
    writer = csv.writer(series, lineterminator="\n")
    writer.writerow(["window_start_s", "window_end_s", "participants", "dispersion", "nss_mean"])
    for start, k, d, m in zip(
        result.window_start_s, result.count, result.dispersion, result.nss_mean, strict=True
    ):
        writer.writerow([_cell(start), _cell(start + result.window_s), int(k), _cell(d), _cell(m)])
    per = io.StringIO()
    writer = csv.writer(per, lineterminator="\n")
    writer.writerow(["participant_code", "window_start_s", "nss"])
    for w, start in enumerate(result.window_start_s):
        for i, code in enumerate(result.participants):
            writer.writerow([code, _cell(start), _cell(result.nss[w, i])])
    atomic_write_bytes(run_dir / SYNCHRONY_FILE, series.getvalue().encode())
    atomic_write_bytes(run_dir / NSS_FILE, per.getvalue().encode())
    atomic_write_bytes(run_dir / SYNCHRONY_PLOT, _plot(result))
    return [SYNCHRONY_FILE, NSS_FILE, SYNCHRONY_PLOT]


def _plot(result: Synchrony) -> bytes:
    figure, top = plt.subplots(figsize=(10, 3.6), dpi=110)
    middle = result.window_start_s + result.window_s / 2
    top.plot(middle, result.dispersion, color="#1e78ff", label="dispersion (video widths)")
    top.set_xlabel("video time (s)")
    top.set_ylabel("dispersion (lower = more alike)", color="#1e78ff")
    bottom = top.twinx()
    bottom.plot(middle, result.nss_mean, color="#d9480f", label="mean NSS")
    bottom.axhline(0, color="#999", linewidth=0.6)
    bottom.set_ylabel("mean NSS (higher = more alike)", color="#d9480f")
    top.set_title(f"Attentional synchrony, {len(result.participants)} participants")
    if not np.isfinite(result.dispersion).any():
        top.text(0.5, 0.5, "fewer than two participants with gaze", transform=top.transAxes,
                 ha="center", va="center", color="#666")  # fmt: skip
    figure.tight_layout()
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png")
    plt.close(figure)
    return buffer.getvalue()
