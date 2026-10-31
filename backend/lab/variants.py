"""Variants of Pipeline A that the product cannot express yet, applied inside the lab only.

- **Feature columns**: the regression is fitted on any columns of `features_a.parquet` (e.g. without
  `face_ratio`, or with the head translation `tx, ty, tz`), through a `PipelineSpec` with other
  `feature_columns`; the product's stages take the spec as an argument, so no product change.
- **High-pass features** (`<column>_hp`): the column minus its own running baseline over
  `window_s` seconds (centred, over usable frames: a face, no blink). The baseline is a trimmed
  mean (`HP_TRIM_FRACTION` cut at each end): robust to half-closed lids the blink rule misses, and,
  unlike a median, it stays between the levels of a signal that alternates between two values
  (looking up, then down), where a median jumps to whichever level is more frequent.
  The idea (E1): lid openness, face size and head angles carry gaze information that changes
  within seconds, and posture or fatigue drift that changes over tens of seconds; the running
  baseline removes the slow part.
  Caveat that the moving-dot test cannot show: when a participant looks at one region of a real
  video for longer than about half the window, the high-pass also removes part of the true gaze
  signal there and pulls the estimate towards the centre.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from numpy.typing import NDArray

from worker.pipeline.features_a import FEATURE_COLUMNS, FEATURE_VECTOR_COLUMNS

Floats = NDArray[np.float64]
HP_SUFFIX = "_hp"
HP_TRIM_FRACTION = 0.1  # cut from each end of a window before averaging
# Columns that are features (not blink scores, which only decide validity).
BASE_COLUMNS = tuple(c for c in FEATURE_COLUMNS if not c.startswith("blink"))


@dataclass(frozen=True)
class Highpass:
    columns: tuple[str, ...]  # base columns to high-pass
    window_s: float


@dataclass(frozen=True)
class Variant:
    """What an experiment changes besides the PipelineConfig."""

    feature_columns: tuple[str, ...] = FEATURE_VECTOR_COLUMNS
    highpass: Highpass | None = None

    def available(self) -> tuple[str, ...]:
        extra = () if self.highpass is None else tuple(c + HP_SUFFIX for c in self.highpass.columns)
        return BASE_COLUMNS + extra


def running_baseline(
    t_s: Floats, x: Floats, use: NDArray[np.bool_], window_s: float, trim: float = HP_TRIM_FRACTION
) -> Floats:
    """Trimmed mean of `x[use]` within `window_s / 2` of each time (NaN where the window has
    none). `t_s` must be increasing."""
    ts, xs = t_s[use & np.isfinite(x)], x[use & np.isfinite(x)]
    half = window_s / 2
    lo = np.searchsorted(ts, t_s - half, side="left")
    hi = np.searchsorted(ts, t_s + half, side="right")
    out = np.full(len(t_s), np.nan)
    for k in range(len(t_s)):
        n = hi[k] - lo[k]
        if n > 0:
            cut = int(n * trim)
            window = np.sort(xs[lo[k] : hi[k]])
            out[k] = float(window[cut : n - cut].mean())
    return out


def highpassed(table: pa.Table, hp: Highpass) -> dict[str, Floats]:
    t = table["pts"].to_numpy().astype(np.float64)
    blink = np.asarray(table["blink"].to_pylist(), dtype=bool)
    face = np.asarray(table["face"].to_pylist(), dtype=bool)
    out = {}
    for c in hp.columns:
        x = np.array([np.nan if v is None else v for v in table[c].to_pylist()], dtype=np.float64)
        out[c + HP_SUFFIX] = x - running_baseline(t, x, face & ~blink, hp.window_s)
    return out


def add_highpass(path: Path, hp: Highpass) -> None:
    """Append the `<column>_hp` columns to a features table, in place (metadata kept)."""
    table = pq.read_table(path)
    metadata = table.schema.metadata
    for name, values in highpassed(table, hp).items():
        table = table.append_column(name, pa.array(values, pa.float64(), mask=np.isnan(values)))
    pq.write_table(table.replace_schema_metadata(metadata), path, compression="snappy")


def check_variant(v: Variant) -> list[str]:
    """Problems with a variant (empty when it is fine)."""
    problems = []
    if not v.feature_columns:
        problems.append("features must name at least one column")
    unknown = [c for c in v.feature_columns if c not in v.available()]
    if unknown:
        problems.append(f"unknown feature columns {unknown} (available: {list(v.available())})")
    if len(set(v.feature_columns)) != len(v.feature_columns):
        problems.append("a feature column is named twice")
    if v.highpass is not None:
        bad = [c for c in v.highpass.columns if c not in BASE_COLUMNS]
        if bad or not v.highpass.columns:
            problems.append(f"highpass.columns must be some of {list(BASE_COLUMNS)}")
        if not v.highpass.window_s > 0:
            problems.append("highpass.window_s must be positive")
    return problems


def replace_with_highpass(columns: Sequence[str], hp_columns: Sequence[str]) -> tuple[str, ...]:
    """`columns` with each of `hp_columns` replaced by its high-passed version."""
    return tuple(c + HP_SUFFIX if c in hp_columns else c for c in columns)
