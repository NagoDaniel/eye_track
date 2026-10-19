"""AOI metrics per participant and area of interest (PLAN §9.3).

An AOI exists from its `start` up to its `end` (cut at the end of the video). Within that time:

- **dwell, samples** (`dwell_sample_s`): the time of the valid samples inside the shape;
- **dwell, fixations** (`dwell_fixation_s`): the time of the fixations whose centroid is inside the
  shape, counting only the part of a fixation that lies in the active time;
- **share** (`share_active`): sample dwell over the active time;
- **time to first fixation** (`time_to_first_fixation_s`): from the AOI's start to the first
  fixation that hits it (empty when none does);
- **fixation count** (`fixation_count`) and **revisits** (`revisits`): an entry is a run of
  consecutive hitting fixations, revisits are the entries after the first.

The shape is grown by the tolerance (k times the participant's validation accuracy, DECISIONS
2026-10-19 item 5). Coordinates are video-normalized.
"""

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from app.aois import PolygonAoi, RectAoi, aoi_contains_many
from app.storage import atomic_write_bytes
from worker.analysis.load import SessionGaze

AOI_LONG_FILE = "aoi_metrics_long.csv"
AOI_WIDE_FILE = "aoi_metrics_wide.csv"
METRICS = (
    "dwell_fixation_s",
    "dwell_sample_s",
    "share_active",
    "time_to_first_fixation_s",
    "fixation_count",
    "revisits",
)
Metrics = dict[str, float | None]


@dataclass(frozen=True)
class AoiRow:
    participant_code: str
    aoi_id: str
    values: Metrics


def tolerance_for(session: SessionGaze, k: float) -> float:
    """k times the participant's validation accuracy in video units; 0 when it is unknown."""
    accuracy = session.accuracy_norm
    return 0.0 if accuracy is None else k * accuracy


def _entries(hits: Sequence[bool]) -> int:
    """The number of runs of consecutive True values."""
    return sum(1 for i, h in enumerate(hits) if h and (i == 0 or not hits[i - 1]))


def aoi_metrics_of(
    session: SessionGaze, aoi: RectAoi | PolygonAoi, tolerance: float, duration_s: float
) -> Metrics:
    """The metrics of one participant for one AOI. `duration_s` is the length of the video."""
    lo, hi = aoi.start, min(aoi.end, duration_s)
    active = hi - lo
    empty: Metrics = {m: 0.0 for m in METRICS}
    empty["share_active"] = None
    empty["time_to_first_fixation_s"] = None
    if active <= 0:
        return empty

    s = session.samples
    in_time = (s.stim_time >= lo) & (s.stim_time < hi)
    inside = aoi_contains_many(aoi, s.x, s.y, tolerance)
    dwell_samples = float(s.dt_s[in_time & inside].sum())

    f = session.fixations
    overlap = np.maximum(0.0, np.minimum(f.stim_end, hi) - np.maximum(f.stim_start, lo))
    overlapped = overlap > 0
    hit = overlapped & aoi_contains_many(aoi, f.x, f.y, tolerance)
    length = f.stim_end - f.stim_start
    with np.errstate(divide="ignore", invalid="ignore"):
        share_of_fixation = np.where(length > 0, overlap / length, 1.0)
    dwell_fixations = float((share_of_fixation * f.duration_ms / 1000.0)[hit].sum())
    first = float(np.min(np.maximum(f.stim_start[hit], lo)) - lo) if hit.any() else None
    entries = _entries([bool(h) for h in hit[overlapped]])
    return {
        "dwell_fixation_s": dwell_fixations,
        "dwell_sample_s": dwell_samples,
        "share_active": dwell_samples / active,
        "time_to_first_fixation_s": first,
        "fixation_count": float(hit.sum()),
        "revisits": float(max(entries - 1, 0)),
    }


def aoi_metrics(
    sessions: Sequence[SessionGaze],
    aois: Sequence[RectAoi | PolygonAoi],
    k: float,
    duration_s: float,
) -> list[AoiRow]:
    """Every participant (in the given order) against every AOI (in file order)."""
    return [
        AoiRow(
            s.participant_code,
            aoi.id,
            aoi_metrics_of(s, aoi, tolerance_for(s, k), duration_s),
        )
        for s in sessions
        for aoi in aois
    ]


def _cell(value: float | None) -> str:
    return "" if value is None else repr(round(value, 6))


def write_metrics(run_dir: Path, rows: Sequence[AoiRow]) -> list[str]:
    """Write the long CSV (`participant_code, aoi_id, metric, value`) and the wide one (one row per
    participant and AOI, a column per metric). Returns the file names."""
    long = io.StringIO()
    writer = csv.writer(long, lineterminator="\n")
    writer.writerow(["participant_code", "aoi_id", "metric", "value"])
    for row in rows:
        for metric in METRICS:
            writer.writerow([row.participant_code, row.aoi_id, metric, _cell(row.values[metric])])
    wide = io.StringIO()
    writer = csv.writer(wide, lineterminator="\n")
    writer.writerow(["participant_code", "aoi_id", *METRICS])
    for row in rows:
        writer.writerow(
            [row.participant_code, row.aoi_id, *(_cell(row.values[m]) for m in METRICS)]
        )
    atomic_write_bytes(run_dir / AOI_LONG_FILE, long.getvalue().encode())
    atomic_write_bytes(run_dir / AOI_WIDE_FILE, wide.getvalue().encode())
    return [AOI_LONG_FILE, AOI_WIDE_FILE]
