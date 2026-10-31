"""E8: more eye signal from MediaPipe, for the lab only.

The product's landmark stage keeps only the two eyeBlink scores of MediaPipe's 52 blendshapes. The
lab runs the same model (the same file, VIDEO mode, every decoded frame, the same timestamps as
`worker/pipeline/landmarks.py`) once per session and caches, per frame:

- every blendshape score as `bs_<name>` (e.g. `bs_eyeLookUpLeft`, `bs_eyeLookDownRight`,
  `bs_eyeLookInLeft`, `bs_eyeLookOutRight`, `bs_eyeSquintLeft`, `bs_eyeWideRight`);
- the iris depth relative to the eye: `iris_l_z`, `iris_r_z` = mean z of the 5 iris landmarks
  minus the mean z of the two eye corners, over the eye width (MediaPipe's z has the scale of x).

`derived/blendshapes.parquet` (`index`, `pts`, then the columns) in the lab's session copy; an
experiment that names one of these columns gets them joined into its features table by `index`.
"""

import math
import sys
from pathlib import Path

import av
import mediapipe as mp
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import FaceLandmarker, FaceLandmarkerOptions, RunningMode

from app.storage import atomic_writer
from lab.layout import REPO_ROOT
from worker.pipeline.features_a import LEFT_EYE, RIGHT_EYE, EyeLandmarks
from worker.pipeline.landmarks import MAX_FACES, _next_timestamp_ms

BLENDSHAPES_FILE = "blendshapes.parquet"
BS_PREFIX = "bs_"
IRIS_Z = ("iris_l_z", "iris_r_z")


def model_file(dest: Path) -> Path:
    """The Face Landmarker model of `shared/models.json` (sha256 checked), downloaded once."""
    sys.path.insert(0, str(REPO_ROOT / "shared"))
    import download_model

    download_model.fetch(download_model.DEFAULT_MANIFEST, ["face_landmarker"], dest)
    return dest / "face_landmarker.task"


def is_extra_column(name: str) -> bool:
    return name.startswith(BS_PREFIX) or name in IRIS_Z


def _iris_z(mesh: list[tuple[float, float, float]], aspect: float, eye: EyeLandmarks) -> float:
    a, b = mesh[eye.corner_a], mesh[eye.corner_b]
    width = math.hypot((b[0] - a[0]) * aspect, b[1] - a[1])
    if width == 0:
        return float("nan")
    iris = float(np.mean([mesh[i][2] for i in eye.iris]))
    return (iris - (a[2] + b[2]) / 2) / width


def extract(model: Path, video: Path, out: Path) -> int:
    """Run the model over every frame of `video`; write `out`; return the number of frames."""
    options = FaceLandmarkerOptions(
        base_options=BaseOptions(model_asset_path=str(model)),
        running_mode=RunningMode.VIDEO,
        num_faces=MAX_FACES,
        output_face_blendshapes=True,
    )
    index: list[int] = []
    pts_list: list[float] = []
    rows: list[dict[str, float]] = []
    names: list[str] = []
    with av.open(str(video)) as container, FaceLandmarker.create_from_options(options) as marker:
        stream = container.streams.video[0]
        time_base = stream.time_base
        if time_base is None:
            raise ValueError(f"{video.name} has no time base")
        timestamp = -1
        for n, frame in enumerate(container.decode(stream)):
            if frame.pts is None:
                raise ValueError(f"frame {n} of {video.name} has no presentation time")
            pts = float(frame.pts * time_base)
            timestamp = _next_timestamp_ms(timestamp, pts)
            rgb = np.ascontiguousarray(frame.to_ndarray(format="rgb24"))
            result = marker.detect_for_video(
                mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), timestamp
            )
            row: dict[str, float] = {}
            if result.face_landmarks and result.face_blendshapes:
                for b in result.face_blendshapes[0]:
                    row[BS_PREFIX + b.category_name] = float(b.score)
                    if not names or len(names) < len(result.face_blendshapes[0]):
                        names = [BS_PREFIX + c.category_name for c in result.face_blendshapes[0]]
                mesh = [(p.x, p.y, p.z) for p in result.face_landmarks[0]]
                aspect = rgb.shape[1] / rgb.shape[0]
                row["iris_l_z"] = _iris_z(mesh, aspect, LEFT_EYE)
                row["iris_r_z"] = _iris_z(mesh, aspect, RIGHT_EYE)
            index.append(n)
            pts_list.append(pts)
            rows.append(row)
    columns = [*names, *IRIS_Z]
    arrays = [pa.array(index, pa.int64()), pa.array(pts_list, pa.float64())]
    for c in columns:
        values = np.array([r.get(c, np.nan) for r in rows], dtype=np.float64)
        arrays.append(pa.array(values.astype(np.float32), pa.float32(), mask=np.isnan(values)))
    table = pa.Table.from_arrays(arrays, names=["index", "pts", *columns])
    with atomic_writer(out) as target:
        pq.write_table(table, target, compression="snappy")
    return len(index)


def join_extra_columns(features: Path, extras: Path, columns: tuple[str, ...]) -> None:
    """Append `columns` of the cached blendshapes table to a features table, row by row (the two
    come from the same decoding, so `index` and `pts` must match; fails loudly otherwise)."""
    table = pq.read_table(features)
    extra = pq.read_table(extras)
    if table["index"].to_pylist() != extra["index"].to_pylist():
        raise ValueError(f"{extras.name} and {features.name} have different frames")
    if not np.allclose(table["pts"].to_numpy(), extra["pts"].to_numpy(), atol=1e-6):
        raise ValueError(f"{extras.name} and {features.name} have different frame times")
    missing = [c for c in columns if c not in extra.column_names]
    if missing:
        raise ValueError(f"{extras.name} has no columns {missing}")
    metadata = table.schema.metadata
    for c in columns:
        table = table.append_column(c, extra[c])
    pq.write_table(table.replace_schema_metadata(metadata), features, compression="snappy")
