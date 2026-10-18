"""Server-side face landmarks (PLAN §8.3): MediaPipe Face Landmarker on every webcam frame.

Decodes the (remuxed) webcam recording at full resolution, runs the model in VIDEO mode with
blendshapes and the facial transformation matrix, and writes `derived/landmarks.parquet`, one row
per decoded frame:

| column | type | meaning |
|---|---|---|
| `index` | int64 | 0-based frame number in decoding order |
| `pts` | float64 | presentation time in seconds |
| `face` | bool | a face was found (the first one, as in the browser) |
| `lm` | fixed list of 478 x 3 float32 | x, y, z per landmark, normalised image coordinates |
| `matrix` | fixed list of 16 float64 | facial transformation matrix, row-major, translation in cm |
| `blink_l`, `blink_r` | float32 | `eyeBlinkLeft` / `eyeBlinkRight` blendshape scores |

`lm`, `matrix` and the blink scores are null in a frame without a face. The file's schema metadata
holds the frame `width` and `height` (the aspect ratio is needed by the Pipeline A features).
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import av
import mediapipe as mp
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import FaceLandmarker, FaceLandmarkerOptions, RunningMode

from app.storage import atomic_writer

LANDMARKS_FILE = "landmarks.parquet"
LANDMARK_COUNT = 478  # 468 mesh points and 2 x 5 iris points
MATRIX_SIZE = 16
# Same as the browser model (`frontend/src/face/faceModel.ts`): up to two faces are looked for and
# the first one is used.
MAX_FACES = 2
BATCH_FRAMES = 500  # rows per parquet row group; bounds memory for a long recording

SCHEMA_FIELDS = [
    pa.field("index", pa.int64()),
    pa.field("pts", pa.float64()),
    pa.field("face", pa.bool_()),
    pa.field("lm", pa.list_(pa.float32(), LANDMARK_COUNT * 3)),
    pa.field("matrix", pa.list_(pa.float64(), MATRIX_SIZE)),
    pa.field("blink_l", pa.float32()),
    pa.field("blink_r", pa.float32()),
]


class LandmarkError(Exception):
    """The landmarks cannot be computed; the message says why."""


@dataclass(frozen=True)
class LandmarkSummary:
    frames: int
    frames_with_face: int
    width: int
    height: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _next_timestamp_ms(previous: int, pts_s: float) -> int:
    return max(round(pts_s * 1000), previous + 1)


def mediapipe_timestamps_ms(pts_s: Sequence[float]) -> list[int]:
    """Strictly increasing integer milliseconds, as VIDEO mode needs, from frame times.

    A frame that is less than 1 ms after the previous one is moved to the next millisecond, so no
    frame is dropped or reordered.
    """
    out: list[int] = []
    previous = -1
    for pts in pts_s:
        previous = _next_timestamp_ms(previous, pts)
        out.append(previous)
    return out


class _Batch:
    """Rows waiting to be written."""

    def __init__(self) -> None:
        self.index: list[int] = []
        self.pts: list[float] = []
        self.face: list[bool] = []
        self.lm: list[np.ndarray[Any, np.dtype[np.float32]] | None] = []
        self.matrix: list[np.ndarray[Any, np.dtype[np.float64]] | None] = []
        self.blink: list[tuple[float, float] | None] = []

    def __len__(self) -> int:
        return len(self.index)

    def table(self, schema: pa.Schema) -> pa.Table:
        n = len(self)
        no_face = ~np.array(self.face, dtype=np.bool_)
        lm = np.zeros((n, LANDMARK_COUNT * 3), dtype=np.float32)
        matrix = np.zeros((n, MATRIX_SIZE), dtype=np.float64)
        blink = np.zeros((n, 2), dtype=np.float32)
        for i in range(n):
            lm_row, matrix_row, blink_row = self.lm[i], self.matrix[i], self.blink[i]
            if lm_row is not None and matrix_row is not None and blink_row is not None:
                lm[i], matrix[i], blink[i] = lm_row, matrix_row, blink_row
        columns = [
            pa.array(self.index, pa.int64()),
            pa.array(self.pts, pa.float64()),
            pa.array(self.face, pa.bool_()),
            pa.FixedSizeListArray.from_arrays(
                pa.array(lm.ravel()), LANDMARK_COUNT * 3, mask=pa.array(no_face)
            ),
            pa.FixedSizeListArray.from_arrays(
                pa.array(matrix.ravel()), MATRIX_SIZE, mask=pa.array(no_face)
            ),
            pa.array(blink[:, 0], pa.float32(), mask=no_face),
            pa.array(blink[:, 1], pa.float32(), mask=no_face),
        ]
        return pa.Table.from_arrays(columns, schema=schema)


def _first_face(
    result: Any,
) -> tuple[np.ndarray[Any, Any], np.ndarray[Any, Any], tuple[float, float]]:
    """Landmarks, matrix and blink scores of the first face; half a result is an error."""
    mesh = result.face_landmarks[0]
    if len(mesh) != LANDMARK_COUNT:
        raise LandmarkError(f"the model returned {len(mesh)} landmarks, expected {LANDMARK_COUNT}")
    if not result.facial_transformation_matrixes:
        raise LandmarkError("a face without a facial transformation matrix")
    scores = {b.category_name: b.score for b in (result.face_blendshapes or [[]])[0]}
    if "eyeBlinkLeft" not in scores or "eyeBlinkRight" not in scores:
        raise LandmarkError("a face without the eyeBlink blendshapes")
    lm = np.array([(p.x, p.y, p.z) for p in mesh], dtype=np.float32).ravel()
    matrix = np.asarray(result.facial_transformation_matrixes[0], dtype=np.float64).ravel()
    if matrix.size != MATRIX_SIZE:
        raise LandmarkError(f"the transformation matrix has {matrix.size} numbers, expected 16")
    return lm, matrix, (scores["eyeBlinkLeft"], scores["eyeBlinkRight"])


def detect_landmarks(model: Path, video: Path, out: Path) -> LandmarkSummary:
    """Run the model over every frame of `video` and write `out`. Raises `LandmarkError`."""
    if not model.is_file():
        raise LandmarkError(f"the face landmarker model {model} does not exist")
    options = FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(model)),
        running_mode=RunningMode.VIDEO,
        num_faces=MAX_FACES,
        output_face_blendshapes=True,
        output_facial_transformation_matrixes=True,
    )
    frames = with_face = 0
    size = (0, 0)
    try:
        with (
            av.open(str(video)) as container,
            FaceLandmarker.create_from_options(options) as landmarker,
            atomic_writer(out) as target,
        ):
            stream = container.streams.video[0]
            time_base = stream.time_base
            if time_base is None:
                raise LandmarkError(f"{video.name} has no time base")
            writer: pq.ParquetWriter | None = None
            batch = _Batch()
            timestamp = -1
            for frame in container.decode(stream):
                if frame.pts is None:
                    raise LandmarkError(f"frame {frames} of {video.name} has no presentation time")
                pts = float(frame.pts * time_base)
                timestamp = _next_timestamp_ms(timestamp, pts)
                rgb = np.ascontiguousarray(frame.to_ndarray(format="rgb24"))
                if writer is None:
                    size = (rgb.shape[1], rgb.shape[0])
                    schema = pa.schema(SCHEMA_FIELDS).with_metadata(
                        {"width": str(size[0]), "height": str(size[1])}
                    )
                    writer = pq.ParquetWriter(target, schema, compression="snappy")
                result = landmarker.detect_for_video(
                    mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp
                )
                batch.index.append(frames)
                batch.pts.append(pts)
                if result.face_landmarks:
                    lm, matrix, blink = _first_face(result)
                    batch.face.append(True)
                    batch.lm.append(lm)
                    batch.matrix.append(matrix)
                    batch.blink.append(blink)
                    with_face += 1
                else:
                    batch.face.append(False)
                    batch.lm.append(None)
                    batch.matrix.append(None)
                    batch.blink.append(None)
                frames += 1
                if len(batch) >= BATCH_FRAMES:
                    writer.write_table(batch.table(writer.schema))
                    batch = _Batch()
            if writer is None:
                raise LandmarkError(f"{video.name} has no video frames")
            if len(batch):
                writer.write_table(batch.table(writer.schema))
            writer.close()
    except av.error.FFmpegError as exc:
        raise LandmarkError(f"{video.name} cannot be read: {exc}") from exc
    return LandmarkSummary(frames=frames, frames_with_face=with_face, width=size[0], height=size[1])
