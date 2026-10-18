"""Pipeline A frame features (PLAN §8.4), the Python twin of `frontend/src/face/features.ts`.

The two implementations must give the same numbers (within 1e-4) on the same input; both are tested
on the frames in `shared/fixtures/face_frames.json` (PLAN §5.6 step 2). Change one, change both.

Input: the `lm` and `matrix` of a feature record (PLAN §5.4) and the frame's aspect ratio
(width / height). Landmarks are normalised image coordinates; to measure distances and angles they
are first scaled to `(x * aspect, y)`, so a unit step is the same in both directions.

Definitions (subject's left/right, as MediaPipe names them):
- Eye-local frame: origin at the midpoint of the two eye corners, x-axis along the corner-to-corner
  vector (for both eyes pointing towards the image's right when the head is upright), y-axis the
  x-axis turned a quarter turn towards the image's bottom, both scaled by the eye width.
- `iris`: mean of the five iris landmarks in that frame, as `(x, y)`.
- `open`: distance between the middle upper and lower lid landmarks, divided by the eye width.
- Head pose from the facial transformation matrix (16 numbers, row-major, translation in indices
  3, 7 and 11, in cm): angles in degrees from `R = Rz(roll) Ry(yaw) Rx(pitch)`. Positive pitch: head
  tilted down; positive yaw: face turned towards the image's right; positive roll: counter-clockwise
  in the image. `tx`, `ty`, `tz` is the translation.
- `face_ratio`: distance between the two eye midpoints as a fraction of the frame width.
"""

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from app.storage import atomic_writer
from worker.pipeline.config import PipelineConfig

# `shared/` sits next to `backend/` in the repository and at `/shared` in the image.
_SUBSET_FILE = Path(__file__).resolve().parents[3] / "shared" / "landmark_subset.json"

MATRIX_SIZE = 16
LANDMARK_COUNT_FULL = 478  # the mesh in landmarks.parquet (worker/pipeline/landmarks.py)


def _load_subset() -> dict[int, int]:
    """Mesh index to position in `lm`: groups in file order, each group in its order."""
    groups = json.loads(_SUBSET_FILE.read_text(encoding="utf-8"))["groups"]
    indices = [i for group in groups.values() for i in group]
    return {mesh_index: position for position, mesh_index in enumerate(indices)}


SUBSET_POSITION = _load_subset()
LANDMARK_COUNT = len(SUBSET_POSITION)

# Mesh indices used below (PLAN §5.4 subset). Corners run towards the image's right.


@dataclass(frozen=True)
class EyeLandmarks:
    corner_a: int
    corner_b: int
    upper_lid: int
    lower_lid: int
    iris: tuple[int, ...]


RIGHT_EYE = EyeLandmarks(33, 133, 159, 145, (468, 469, 470, 471, 472))
LEFT_EYE = EyeLandmarks(362, 263, 386, 374, (473, 474, 475, 476, 477))


@dataclass(frozen=True)
class FrameFeatures:
    iris_l: tuple[float, float]
    iris_r: tuple[float, float]
    open_l: float
    open_r: float
    yaw: float
    pitch: float
    roll: float
    tx: float
    ty: float
    tz: float
    face_ratio: float

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


Point = tuple[float, float]


def _eye(
    lm: Sequence[Sequence[float]], aspect: float, eye: EyeLandmarks
) -> tuple[Point, float, Point]:
    """Return (iris position in the eye-local frame, openness, eye centre) of one eye."""

    def point(mesh_index: int) -> Point:
        x, y = lm[SUBSET_POSITION[mesh_index]][:2]
        return x * aspect, y

    a, b = point(eye.corner_a), point(eye.corner_b)
    centre = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    dx, dy = b[0] - a[0], b[1] - a[1]
    width = math.hypot(dx, dy)
    if width == 0:
        raise ValueError("the eye corners coincide")
    ex, ey = dx / width, dy / width
    iris_points = [point(i) for i in eye.iris]
    ix = sum(p[0] for p in iris_points) / len(iris_points) - centre[0]
    iy = sum(p[1] for p in iris_points) / len(iris_points) - centre[1]
    # x-axis (ex, ey); y-axis turned towards the image's bottom: (-ey, ex)
    iris = ((ix * ex + iy * ey) / width, (-ix * ey + iy * ex) / width)
    upper, lower = point(eye.upper_lid), point(eye.lower_lid)
    openness = math.hypot(upper[0] - lower[0], upper[1] - lower[1]) / width
    return iris, openness, centre


def head_pose(matrix: Sequence[float]) -> tuple[float, float, float, float, float, float]:
    """`(yaw, pitch, roll, tx, ty, tz)` of a row-major 4x4 transformation matrix, angles in degrees.

    The scale of the matrix is removed first (each column of the rotation part is normalised).
    """
    if len(matrix) != MATRIX_SIZE:
        raise ValueError(f"the matrix needs {MATRIX_SIZE} numbers, got {len(matrix)}")
    r = [[matrix[row * 4 + col] for col in range(3)] for row in range(3)]
    for col in range(3):
        norm = math.sqrt(sum(r[row][col] ** 2 for row in range(3)))
        if norm == 0:
            raise ValueError("the rotation part of the matrix is degenerate")
        for row in range(3):
            r[row][col] /= norm
    yaw = math.asin(max(-1.0, min(1.0, -r[2][0])))
    pitch = math.atan2(r[2][1], r[2][2])
    roll = math.atan2(r[1][0], r[0][0])
    return (
        math.degrees(yaw),
        math.degrees(pitch),
        math.degrees(roll),
        matrix[3],
        matrix[7],
        matrix[11],
    )


def frame_features(
    lm: Sequence[Sequence[float]], matrix: Sequence[float], aspect: float
) -> FrameFeatures:
    """The Pipeline A features of one frame with a face. Raises ValueError on bad input."""
    if len(lm) != LANDMARK_COUNT:
        raise ValueError(f"lm needs {LANDMARK_COUNT} landmarks, got {len(lm)}")
    if aspect <= 0:
        raise ValueError("aspect must be positive")
    iris_r, open_r, centre_r = _eye(lm, aspect, RIGHT_EYE)
    iris_l, open_l, centre_l = _eye(lm, aspect, LEFT_EYE)
    yaw, pitch, roll, tx, ty, tz = head_pose(matrix)
    between = math.hypot(centre_l[0] - centre_r[0], centre_l[1] - centre_r[1])
    return FrameFeatures(
        iris_l=iris_l,
        iris_r=iris_r,
        open_l=open_l,
        open_r=open_r,
        yaw=yaw,
        pitch=pitch,
        roll=roll,
        tx=tx,
        ty=ty,
        tz=tz,
        face_ratio=between / aspect,
    )


# --- The stage: landmarks.parquet to features_a.parquet (PLAN §8.4) -------------------------------

FEATURES_FILE = "features_a.parquet"
# Mesh indices of the 49-landmark subset, in the order of a feature record's `lm`.
SUBSET_MESH_INDICES = sorted(SUBSET_POSITION, key=SUBSET_POSITION.__getitem__)

# Float columns that are null in a frame without a face, in the order they are written.
FEATURE_COLUMNS = (
    "iris_l_x", "iris_l_y", "iris_r_x", "iris_r_y", "open_l", "open_r",
    "yaw", "pitch", "roll", "tx", "ty", "tz", "face_ratio", "blink_l", "blink_r",
)  # fmt: skip


def is_blink(
    blink_l: float, blink_r: float, open_l: float, open_r: float, config: PipelineConfig
) -> bool:
    """PLAN §8.4: an eyeBlink blendshape above the threshold in either eye, or either eye's
    openness below its threshold. The limits themselves are not blinks."""
    return (
        blink_l > config.blink_score_threshold
        or blink_r > config.blink_score_threshold
        or open_l < config.blink_openness_threshold
        or open_r < config.blink_openness_threshold
    )


@dataclass(frozen=True)
class FeatureSummary:
    frames: int
    frames_with_face: int
    blinks: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_features(landmarks: Path, out: Path, config: PipelineConfig) -> FeatureSummary:
    """Pipeline A features of every frame of `landmarks.parquet` (see `landmarks.py`).

    One row per landmark row, with `index` and `pts` copied. `face` is false and every feature null
    in a frame without a face; `blink` is then false (such a frame is invalid for another reason).
    `blink_l` and `blink_r` are the blendshape scores. The thresholds used go into the file's
    metadata as `config`. Raises ValueError when the frame size is missing from the metadata.
    """
    table = pq.read_table(landmarks)
    metadata = table.schema.metadata or {}
    try:
        aspect = int(metadata[b"width"]) / int(metadata[b"height"])
    except KeyError as exc:
        raise ValueError(f"{landmarks.name} has no frame width and height in its metadata") from exc

    n = table.num_rows
    face = np.array(table["face"].to_pylist(), dtype=np.bool_)
    rows = np.flatnonzero(face)
    values = np.full((n, len(FEATURE_COLUMNS)), np.nan)
    blink = np.zeros(n, dtype=np.bool_)
    if len(rows):
        faces = table.take(rows)
        mesh = (
            faces["lm"].combine_chunks().values.to_numpy(zero_copy_only=False)
            .reshape(len(rows), LANDMARK_COUNT_FULL, 3)
        )  # fmt: skip
        subset = mesh[:, SUBSET_MESH_INDICES, :]
        matrices = faces["matrix"].combine_chunks().values.to_numpy(zero_copy_only=False)
        matrices = matrices.reshape(len(rows), MATRIX_SIZE)
        scores = np.stack(
            [faces["blink_l"].to_numpy(zero_copy_only=False), faces["blink_r"].to_numpy()], axis=1
        )
        for k, row in enumerate(rows):
            f = frame_features(subset[k], matrices[k], aspect)
            values[row] = (
                *f.iris_l, *f.iris_r, f.open_l, f.open_r, f.yaw, f.pitch, f.roll,
                f.tx, f.ty, f.tz, f.face_ratio, scores[k, 0], scores[k, 1],
            )  # fmt: skip
            blink[row] = is_blink(scores[k, 0], scores[k, 1], f.open_l, f.open_r, config)

    no_face = ~face
    columns = [table["index"], table["pts"], table["face"]]
    columns += [pa.array(values[:, i], pa.float64(), mask=no_face) for i in range(values.shape[1])]
    columns.append(pa.array(blink))
    names = ["index", "pts", "face", *FEATURE_COLUMNS, "blink"]
    result = pa.Table.from_arrays(columns, names=names).replace_schema_metadata(
        {
            "config": json.dumps(config.as_dict()),
            "width": metadata[b"width"].decode(),
            "height": metadata[b"height"].decode(),
        }
    )
    with atomic_writer(out) as target:
        pq.write_table(result, target, compression="snappy")
    return FeatureSummary(frames=n, frames_with_face=len(rows), blinks=int(blink.sum()))
