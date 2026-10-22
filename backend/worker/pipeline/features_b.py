"""Pipeline B features (PLAN §8.5): the gaze angles of a neural network plus A's head pose.

Per webcam frame with a face: a square crop around the landmark box (the longer side of the box
of all 478 landmarks times `b_crop_scale`, DECISIONS 2026-10-20), resized to 448 x 448 and run
through the gaze network (`model_b.py`), which gives pitch and yaw in radians. The crop rule is
fixed: the calibration absorbs a constant difference, not one that changes between runs.

`derived/features_b.parquet`, one row per webcam frame (the rows of `landmarks.parquet` and
`features_a.parquet`):

| column | type | meaning |
|---|---|---|
| `index`, `pts`, `face` | | as in `features_a.parquet` |
| `gaze_pitch`, `gaze_yaw` | float64 | the network's angles in radians; null without a face |
| `yaw`, `pitch`, `roll`, `face_ratio` | float64 | A's head pose and face size; null without face |
| `blink` | bool | A's blink flag, copied (B's blinks and validity are A's) |
"""

import json
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import av
import cv2
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from numpy.typing import NDArray

from app.storage import atomic_writer
from worker.cancel import check_cancelled
from worker.pipeline.config import PipelineConfig
from worker.pipeline.spec import SPEC_B

FEATURES_B_FILE = SPEC_B.features_file
INPUT_SIZE = 448  # the network's input (PLAN §8.5)
FEATURES_B_VERSION = 1
HEAD_COLUMNS = ("yaw", "pitch", "roll", "face_ratio")

Floats = NDArray[np.float64]
Image = NDArray[np.uint8]


class FeaturesBError(Exception):
    """The inputs of the stage do not fit together."""


class GazeEstimator(Protocol):
    """What the stage needs of the network: RGB crops of `INPUT_SIZE` in, radians out."""

    def predict(self, images: Sequence[Image], batch_size: int = 8) -> tuple[Floats, Floats]:
        """`(pitch, yaw)` for each image."""
        ...


def face_boxes(
    lm: NDArray[np.float32], width: int, height: int, scale: float
) -> NDArray[np.float64]:
    """Square crops for faces given as `(faces, 478, 3)` landmarks in normalised image coordinates:
    `(cx, cy, side)` in px per face. The side is the longer side of the landmark box times
    `scale`."""
    x = lm[:, :, 0].astype(np.float64) * width
    y = lm[:, :, 1].astype(np.float64) * height
    x0, x1, y0, y1 = x.min(axis=1), x.max(axis=1), y.min(axis=1), y.max(axis=1)
    side = np.maximum(x1 - x0, y1 - y0) * scale
    return np.stack([(x0 + x1) / 2, (y0 + y1) / 2, side], axis=1)


def crop_square(rgb: Image, cx: float, cy: float, side: float, size: int = INPUT_SIZE) -> Image:
    """The square of `side` px centred on (cx, cy), resized to `size` x `size`. Where the square
    leaves the frame the crop is black."""
    factor = size / side
    matrix = np.array(
        [[factor, 0.0, size / 2 - cx * factor], [0.0, factor, size / 2 - cy * factor]]
    )
    crop = cv2.warpAffine(
        rgb, matrix, (size, size), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT
    )
    return np.asarray(crop, dtype=np.uint8)


@dataclass(frozen=True)
class FeaturesBSummary:
    frames: int
    frames_with_face: int
    seconds_per_face: float | None  # the network's time per face, for planning (DECISIONS)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _decode(video: Path) -> Iterator[tuple[int, float, Image]]:
    with av.open(str(video)) as container:
        stream = container.streams.video[0]
        time_base = stream.time_base
        if time_base is None:
            raise FeaturesBError(f"{video.name} has no time base")
        for n, frame in enumerate(container.decode(stream)):
            if frame.pts is None:
                raise FeaturesBError(f"frame {n} of {video.name} has no presentation time")
            yield (
                n,
                float(frame.pts * time_base),
                np.ascontiguousarray(frame.to_ndarray(format="rgb24")),
            )


def compute_features_b(
    video: Path,
    landmarks: Path,
    features_a: Path,
    out: Path,
    model: GazeEstimator,
    model_info: dict[str, Any],
    config: PipelineConfig,
) -> FeaturesBSummary:
    """Write `out`. Raises `FeaturesBError` when the video, the landmarks and A's features do not
    describe the same frames."""
    import time

    marks = pq.read_table(landmarks, columns=["index", "pts", "face", "lm"])
    a = pq.read_table(features_a)
    n = marks.num_rows
    if a.num_rows != n:
        raise FeaturesBError(f"features_a has {a.num_rows} rows, landmarks {n}")
    if not np.allclose(marks["pts"].to_numpy(), a["pts"].to_numpy(), atol=1e-9):
        raise FeaturesBError("landmarks and features_a have different frame times")
    meta = pq.read_schema(landmarks).metadata or {}
    width, height = int(meta[b"width"]), int(meta[b"height"])
    face = np.asarray(marks["face"].to_pylist(), dtype=np.bool_)
    lm = np.asarray([v for v in marks["lm"].to_pylist() if v is not None], dtype=np.float32)
    lm = lm.reshape(-1, 478, 3)
    boxes = face_boxes(lm, width, height, config.b_crop_scale)
    face_row = np.cumsum(face) - 1  # row of a frame in `boxes`

    pitch = np.full(n, np.nan)
    yaw = np.full(n, np.nan)
    pending: list[tuple[int, Image]] = []
    network_seconds = 0.0

    def run_batch() -> None:
        nonlocal network_seconds
        if not pending:
            return
        check_cancelled()
        started = time.perf_counter()
        p, y = model.predict([crop for _, crop in pending], config.b_batch_size)
        network_seconds += time.perf_counter() - started
        for (row, _), pp, yy in zip(pending, p, y, strict=True):
            pitch[row], yaw[row] = pp, yy
        pending.clear()

    decoded = 0
    for row, pts, rgb in _decode(video):
        if row >= n:
            raise FeaturesBError(f"{video.name} has more frames than landmarks.parquet ({n})")
        if abs(pts - float(marks["pts"][row].as_py())) > 1e-6:
            raise FeaturesBError(f"frame {row} of {video.name} has another time than landmarks")
        decoded += 1
        if face[row]:
            cx, cy, side = boxes[face_row[row]]
            pending.append((row, crop_square(rgb, float(cx), float(cy), float(side))))
            if len(pending) >= config.b_batch_size:
                run_batch()
    run_batch()
    if decoded != n:
        raise FeaturesBError(f"{video.name} has {decoded} frames, landmarks.parquet {n}")

    def nullable(values: Floats) -> pa.Array:
        return pa.array(values, pa.float64(), mask=np.isnan(values))

    columns = [a["index"], a["pts"], a["face"], nullable(pitch), nullable(yaw)]
    columns += [a[c] for c in HEAD_COLUMNS] + [a["blink"]]
    names = ["index", "pts", "face", "gaze_pitch", "gaze_yaw", *HEAD_COLUMNS, "blink"]
    info = {
        "version": FEATURES_B_VERSION,
        "model": model_info,
        "crop_scale": config.b_crop_scale,
        "input_size": INPUT_SIZE,
        "batch_size": config.b_batch_size,
    }
    table = pa.Table.from_arrays(columns, names=names).replace_schema_metadata(
        {"features_b": json.dumps(info)}
    )
    with atomic_writer(out) as target:
        pq.write_table(table, target, compression="snappy")
    faces = int(face.sum())
    return FeaturesBSummary(n, faces, network_seconds / faces if faces else None)
