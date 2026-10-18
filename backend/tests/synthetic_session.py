"""A session on disk whose gaze is known (for the stages after the calibration).

The derived files up to `calibration_a.json` are written directly, with a calibration model that
maps the first two features to x and y in px (x = 1000 * iris_l_x, y = 1000 * iris_l_y), so the
predicted gaze is exactly the designed scanpath plus a drift that grows from block to block. The
timeline (client ms, 30 fps frames, viewport 1000 x 600; dots last 1500 ms and are clicked
1000 ms after they appear, as in a real session):

    1000-4000   2 calibration dots           4000-11100  validation block (5 dots)
    12000-20100 a video in the rectangle (100, 0, 800 x 600), frames every 40 ms from 12100
    21000-28100 validation block after the video
"""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from app.storage import derived_dir, events_path, stimulus_frames_path
from worker.pipeline.features_a import FEATURE_COLUMNS
from worker.pipeline.ridge import RidgeModel

W, H = 1000.0, 600.0
FRAME_MS = 1000 / 30
VIDEO_RECT = {"x": 100.0, "y": 0.0, "width": 800.0, "height": 600.0}
VIDEO_START_MS = 12_000.0
VIDEO_END_MS = 20_100.0
VIDEO_FIRST_FRAME_MS = 12_100.0
VALIDATION_STARTS = (4000.0, 21_000.0)
TARGETS = [(500.0, 300.0), (250.0, 150.0), (750.0, 150.0), (250.0, 450.0), (750.0, 450.0)]
# (start, end, x, y): the fixations of the playback; a 60 ms saccade joins consecutive ones.
FIXATIONS = [
    (12_000.0, 12_440.0, 500.0, 300.0),
    (12_500.0, 13_300.0, 300.0, 300.0),
    (13_360.0, 15_000.0, 700.0, 200.0),
    (15_400.0, 16_200.0, 500.0, 450.0),
    (16_260.0, 20_100.0, 500.0, 300.0),
]
BLINK = (13_000.0, 13_090.0)  # inside the second fixation
FACE_LOST = (15_000.0, 15_400.0)  # between the third and the fourth
DRIFT_FIRST, DRIFT_LAST = 10.0, 30.0  # px in x at the two validation blocks
# The mean click time of the five dots of a block: the time the drift model assigns to it.
BLOCK_TIMES = tuple(start + 2 * 1500 + 1000 for start in VALIDATION_STARTS)


def drift_at(t: np.ndarray) -> np.ndarray:
    """The designed offset in x: linear between the two validation blocks, constant outside."""
    return np.asarray(np.interp(t, BLOCK_TIMES, [DRIFT_FIRST, DRIFT_LAST]))


@dataclass(frozen=True)
class Synthetic:
    root: Path
    study_id: int
    session_id: uuid.UUID
    t: np.ndarray
    truth_x: np.ndarray
    truth_y: np.ndarray


def ev(t: float, type_: str, **data: object) -> dict[str, Any]:
    return {"t": t, "type": type_, "data": data}


def viewport(t: float, video_rect: dict[str, float] | None) -> dict[str, Any]:
    size = {"width": W, "height": H}
    return ev(t, "viewport_change", viewport=size, screen=size, device_pixel_ratio=1,
              video_rect=video_rect)  # fmt: skip


def dot_events(
    t0: float, phase: str, positions: list[tuple[float, float]]
) -> list[dict[str, object]]:
    out = []
    for i, (x, y) in enumerate(positions):
        t = t0 + i * 1500
        out += [
            ev(t, "calib_dot_show", phase=phase, id=i, x=x, y=y),
            ev(t + 1000, "calib_dot_click", phase=phase, id=i),
            ev(t + 1100, "calib_dot_hide", phase=phase, id=i),
        ]
    return out


def truth_at(t: np.ndarray, events: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    """Where the participant looks at each time."""
    x, y = np.full(len(t), 500.0), np.full(len(t), 300.0)
    # dots: the target while it is shown
    shown: dict[tuple[str, int], tuple[float, float, float]] = {}
    for e in events:
        d = e["data"]
        if e["type"] == "calib_dot_show":
            shown[(d["phase"], d["id"])] = (e["t"], d["x"], d["y"])
        elif e["type"] == "calib_dot_hide":
            start, px, py = shown[(d["phase"], d["id"])]
            inside = (t >= start) & (t < e["t"])
            x[inside], y[inside] = px, py
    # the scanpath: fixations and the saccades between them
    for k, (start, end, fx, fy) in enumerate(FIXATIONS):
        inside = (t >= start) & (t <= end)
        x[inside], y[inside] = fx, fy
        if k + 1 < len(FIXATIONS):
            nxt = FIXATIONS[k + 1]
            between = (t > end) & (t < nxt[0])
            fraction = (t[between] - end) / (nxt[0] - end)
            x[between] = fx + (nxt[2] - fx) * fraction
            y[between] = fy + (nxt[3] - fy) * fraction
    return x, y


def build(
    root: Path, with_validation: bool = True, noise_px: float = 2.0, seed: int = 0
) -> Synthetic:
    study_id, session_id = 1, uuid.UUID(int=7)
    derived = derived_dir(root, study_id, session_id)
    derived.mkdir(parents=True)
    events_file = events_path(root, study_id, session_id)
    events_file.parent.mkdir(parents=True, exist_ok=True)

    events: list[dict[str, Any]] = [
        viewport(0, None),
        ev(500, "recorder_start", mime_type="video/webm", bits_per_second=1, settings={}),
    ]  # fmt: skip
    events += dot_events(1000, "calibration", [(100.0 + 60 * i, 100.0 + 30 * i) for i in range(2)])
    if with_validation:
        events += dot_events(VALIDATION_STARTS[0], "validation", TARGETS)
    events += [
        ev(VIDEO_START_MS, "video_load", video_id="1", duration=8, width=800, height=600,
           rect=VIDEO_RECT),  # fmt: skip
        viewport(VIDEO_START_MS, VIDEO_RECT),
        ev(VIDEO_END_MS, "video_ended", video_id="1"),
        viewport(VIDEO_END_MS, None),
    ]  # fmt: skip
    if with_validation:
        events += dot_events(VALIDATION_STARTS[1], "validation", TARGETS)
    events.sort(key=lambda e: float(e["t"]))
    events_file.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    frames = [
        {
            "t": VIDEO_FIRST_FRAME_MS + 40 * k + 5,
            "expected_display_time": VIDEO_FIRST_FRAME_MS + 40 * k,
            "media_time": 0.04 * k,
            "presented_frames": k + 1,
        }
        for k in range(200)
    ]
    stimulus_frames_path(root, study_id, session_id).write_text(
        "".join(json.dumps(f) + "\n" for f in frames), encoding="utf-8"
    )

    t = np.arange(0.0, 29_500.0, FRAME_MS)
    truth_x, truth_y = truth_at(t, events)
    # Drift: the gaze is off by an offset that grows linearly between the two blocks.
    offset_x = drift_at(t)
    rng = np.random.default_rng(seed)
    gaze_x = truth_x + offset_x + rng.normal(0, noise_px, len(t))
    gaze_y = truth_y + rng.normal(0, noise_px, len(t))

    face = ~((t >= FACE_LOST[0]) & (t < FACE_LOST[1]))
    blink = (t >= BLINK[0]) & (t < BLINK[1])
    columns: dict[str, np.ndarray] = {c: np.full(len(t), 0.3) for c in FEATURE_COLUMNS}
    columns["iris_l_x"], columns["iris_l_y"] = gaze_x / 1000, gaze_y / 1000
    columns["blink_l"] = np.where(blink, 0.9, 0.05)
    columns["blink_r"] = np.where(blink, 0.9, 0.05)
    mask = ~face
    arrays = [
        pa.array(np.arange(len(t)), pa.int64()), pa.array(t / 1000, pa.float64()), pa.array(face),
        *[pa.array(columns[c], pa.float64(), mask=mask) for c in FEATURE_COLUMNS],
        pa.array(blink & face),
    ]  # fmt: skip
    table = pa.Table.from_arrays(arrays, names=["index", "pts", "face", *FEATURE_COLUMNS, "blink"])
    pq.write_table(
        table.replace_schema_metadata({"width": "854", "height": "480"}),
        derived / "features_a.parquet",
    )

    (derived / "sync.json").write_text(
        json.dumps({"version": 1, "t_of_pts": {"a_ms": 0.0, "b": 1.0}}), encoding="utf-8"
    )
    weights = np.zeros((10, 2))
    weights[0, 0], weights[1, 1] = 1000.0, 1000.0
    model = RidgeModel(1.0, 1, np.zeros(10), np.ones(10), weights, np.zeros(2))
    (derived / "calibration_a.json").write_text(
        json.dumps(
            {
                "status": "ok", "failed_reason": None,
                "feature_columns": [
                    "iris_l_x", "iris_l_y", "iris_r_x", "iris_r_y", "open_l", "open_r",
                    "yaw", "pitch", "roll", "face_ratio",
                ],
                "attempt_used": {"attempt": 1, "index": 0, "passed": True, "reason": "only_attempt",
                                 "start_t": 1000.0},
                "model": model.as_dict(),
            }
        ),
        encoding="utf-8",
    )  # fmt: skip
    return Synthetic(root, study_id, session_id, t, truth_x, truth_y)
