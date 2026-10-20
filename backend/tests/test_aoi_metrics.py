"""AOI metrics against hand-computed values (PLAN §9.3, §13)."""

import csv
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from pydantic import TypeAdapter

from app.aois import Aoi, PolygonAoi, RectAoi
from worker.analysis.aoi_metrics import (
    AOI_LONG_FILE,
    AOI_WIDE_FILE,
    METRICS,
    AoiRow,
    aoi_metrics,
    aoi_metrics_of,
    tolerance_for,
    write_metrics,
)
from worker.analysis.load import Fixations, Samples, SessionGaze

ADAPTER: TypeAdapter[RectAoi | PolygonAoi] = TypeAdapter(Aoi)


def aoi(
    id_: str, coords: list[float], start: float = 1.0, end: float = 8.0
) -> RectAoi | PolygonAoi:
    return ADAPTER.validate_python(
        {"id": id_, "label": id_, "start": start, "end": end, "shape": "rect", "coords": coords}
    )


INSIDE = (0.4, 0.4)
OUTSIDE = (0.9, 0.9)
# (start, end, where): see the list of expected values in the tests.
FIXATIONS = [
    (0.2, 0.7, INSIDE),  # before the AOI exists: not counted at all
    (0.8, 1.4, INSIDE),  # starts before the AOI: 0.4 s of it count
    (1.5, 2.0, OUTSIDE),
    (2.5, 3.5, INSIDE),
    (3.6, 4.0, INSIDE),  # directly after the one before: the same entry
    (5.0, 5.5, OUTSIDE),
    (6.0, 7.0, INSIDE),
    (7.5, 8.5, INSIDE),  # ends after the AOI: 0.5 s count; same entry as the one before
    (9.0, 9.5, INSIDE),  # after the AOI is gone
]


def participant(code: str = "P1", accuracy_px: float | None = 60.0) -> SessionGaze:
    t = np.round(np.arange(0.0, 10.0, 0.1), 6)
    x, y = np.full(len(t), 0.9), np.full(len(t), 0.9)
    inside = ((t >= 2.0) & (t < 5.0)) | (np.abs(t - 0.5) < 1e-9) | (np.abs(t - 8.2) < 1e-9)
    x[inside], y[inside] = INSIDE
    near = (t >= 6.0) & (t < 6.5)  # just right of the rectangle (x 0.6): 0.05 away
    x[near], y[near] = 0.65, 0.4
    samples = Samples(t * 1000, t, x, y, np.full(len(t), 0.1))
    f = np.array([(a, b, *where) for a, b, where in FIXATIONS])
    fixations = Fixations(
        start_t=f[:, 0] * 1000, end_t=f[:, 1] * 1000, stim_start=f[:, 0], stim_end=f[:, 1],
        duration_ms=(f[:, 1] - f[:, 0]) * 1000, x=f[:, 2], y=f[:, 3],
    )  # fmt: skip
    return SessionGaze(uuid.uuid4(), code, samples, fixations, accuracy_px, 1000.0)


AREA = aoi("a", [0.2, 0.2, 0.6, 0.6])  # exists from 1 s to 8 s


def test_fixation_metrics_by_hand() -> None:
    m = aoi_metrics_of(participant(), AREA, 0.0, 10.0)
    assert m["fixation_count"] == 5  # 0.8-1.4, 2.5-3.5, 3.6-4.0, 6-7, 7.5-8.5
    assert m["dwell_fixation_s"] == pytest.approx(0.4 + 1.0 + 0.4 + 1.0 + 0.5)
    assert m["time_to_first_fixation_s"] == pytest.approx(0.0)  # the first one was there at once
    assert m["revisits"] == 2  # entries: [0.8-1.4], [2.5-3.5, 3.6-4.0], [6-7, 7.5-8.5]


def test_sample_dwell_and_share_by_hand() -> None:
    m = aoi_metrics_of(participant(), AREA, 0.0, 10.0)
    # 30 samples (2.0 to 4.9 s) of 0.1 s; those at 0.5 and 8.2 s are outside the active time
    assert m["dwell_sample_s"] == pytest.approx(3.0)
    assert m["share_active"] == pytest.approx(3.0 / 7.0)


def test_tolerance_grows_the_shape_for_samples_and_fixations() -> None:
    p = participant()
    assert tolerance_for(p, 1.0) == pytest.approx(0.06)  # 60 px of a 1000 px wide video
    assert tolerance_for(p, 0.0) == 0.0
    assert tolerance_for(participant(accuracy_px=None), 2.0) == 0.0
    grown = aoi_metrics_of(p, AREA, tolerance_for(p, 1.0), 10.0)
    assert grown["dwell_sample_s"] == pytest.approx(3.5)  # + the 5 samples 0.05 right of the edge


def test_the_tolerance_is_the_same_distance_on_screen_vertically_and_horizontally() -> None:
    # The samples at y 0.65 are 0.05 right of the rectangle (x 0.6); a 16:9 video is 0.5625 as high
    # as wide, so 0.06 video widths reach 0.06 / 0.5625 = 0.107 of the height upwards.
    above = aoi("above", [0.2, 0.2, 0.6, 0.6])
    p = participant()
    assert aoi_metrics_of(p, above, 0.06, 10.0, aspect=16 / 9)["dwell_sample_s"] == pytest.approx(
        3.5
    )
    low = Samples(
        np.array([5000.0]), np.array([5.0]), np.array([0.4]), np.array([0.69]), np.array([0.1])
    )
    q = SessionGaze(uuid.uuid4(), "Q", low, p.fixations, 60.0, 1000.0)
    assert aoi_metrics_of(q, above, 0.06, 10.0, aspect=16 / 9)["dwell_sample_s"] == pytest.approx(
        0.1
    )
    assert aoi_metrics_of(q, above, 0.06, 10.0, aspect=1.0)["dwell_sample_s"] == 0.0  # 0.09 > 0.06


def test_a_fixation_only_counts_when_its_centroid_is_inside() -> None:
    near = aoi("n", [0.45, 0.2, 0.9, 0.6])  # 0.4 is 0.05 left of this one
    assert aoi_metrics_of(participant(), near, 0.0, 10.0)["fixation_count"] == 0
    assert aoi_metrics_of(participant(), near, 0.06, 10.0)["fixation_count"] == 5


def test_an_area_that_is_never_hit_has_zeros_and_no_first_fixation() -> None:
    m = aoi_metrics_of(participant(), aoi("b", [0.7, 0.0, 0.8, 0.1]), 0.0, 10.0)
    assert m["fixation_count"] == 0 and m["revisits"] == 0 and m["dwell_fixation_s"] == 0
    assert m["dwell_sample_s"] == 0 and m["share_active"] == 0
    assert m["time_to_first_fixation_s"] is None


def test_time_to_first_fixation_is_counted_from_the_start_of_the_area() -> None:
    late = aoi("late", [0.2, 0.2, 0.6, 0.6], start=2.0, end=8.0)
    m = aoi_metrics_of(participant(), late, 0.0, 10.0)
    assert m["time_to_first_fixation_s"] == pytest.approx(0.5)  # 2.5 - 2.0
    assert m["fixation_count"] == 4 and m["revisits"] == 1


def test_the_active_time_is_cut_at_the_end_of_the_video() -> None:
    long = aoi("long", [0.2, 0.2, 0.6, 0.6], start=1.0, end=100.0)
    m = aoi_metrics_of(participant(), long, 0.0, 10.0)
    assert m["share_active"] == pytest.approx(3.1 / 9.0)  # 1 to 10 s; the sample at 8.2 counts
    beyond = aoi("beyond", [0.2, 0.2, 0.6, 0.6], start=12.0, end=20.0)
    m = aoi_metrics_of(participant(), beyond, 0.0, 10.0)
    assert m["share_active"] is None and m["fixation_count"] == 0


def test_every_participant_meets_every_area_in_order() -> None:
    rows = aoi_metrics(
        [participant("P1"), participant("P2")], [AREA, aoi("b", [0.7, 0, 0.8, 0.1])], 0.0, 10.0
    )
    assert [(r.participant_code, r.aoi_id) for r in rows] == [
        ("P1", "a"), ("P1", "b"), ("P2", "a"), ("P2", "b")
    ]  # fmt: skip


def test_the_csv_files_hold_the_same_numbers(tmp_path: Path) -> None:
    rows = aoi_metrics([participant("P1")], [AREA, aoi("b", [0.7, 0, 0.8, 0.1])], 0.0, 10.0)
    assert write_metrics(tmp_path, rows) == [AOI_LONG_FILE, AOI_WIDE_FILE]

    def read(name: str) -> list[dict[str, Any]]:
        with (tmp_path / name).open() as f:
            return list(csv.DictReader(f))

    long = read(AOI_LONG_FILE)
    assert len(long) == 2 * len(METRICS)
    assert long[0] == {"participant_code": "P1", "aoi_id": "a", "metric": "dwell_fixation_s",
                       "value": "3.3"}  # fmt: skip
    wide = read(AOI_WIDE_FILE)
    assert [r["aoi_id"] for r in wide] == ["a", "b"]
    assert float(wide[0]["dwell_sample_s"]) == pytest.approx(3.0)
    assert wide[1]["time_to_first_fixation_s"] == ""  # never hit: empty, not 0
    assert wide[1]["fixation_count"] == "0.0"
    assert list(wide[0]) == ["participant_code", "aoi_id", *METRICS]
    assert not list(tmp_path.glob("*.tmp"))


def test_aoi_row_is_plain_data() -> None:
    assert AoiRow("P", "a", {m: 0.0 for m in METRICS}).aoi_id == "a"
