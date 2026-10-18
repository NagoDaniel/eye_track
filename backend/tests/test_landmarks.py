"""Server-side landmarks (PLAN §8.3) with the real MediaPipe Face Landmarker and the real PyAV."""

import json
import subprocess
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tests.session_fixture import FixtureSession, copy_fixture_session
from worker.pipeline.features_a import SUBSET_POSITION
from worker.pipeline.ingest import ingest
from worker.pipeline.landmarks import (
    LANDMARK_COUNT,
    LANDMARKS_FILE,
    LandmarkError,
    detect_landmarks,
    mediapipe_timestamps_ms,
)

REPO = Path(__file__).resolve().parents[2]
FACE_CLIP = REPO / "e2e" / "fixtures" / "face.mp4"
FACE_FRAMES = REPO / "shared" / "fixtures" / "face_frames.json"


def test_timestamps_are_strictly_increasing_milliseconds() -> None:
    # MediaPipe's VIDEO mode needs strictly increasing integer milliseconds; frames closer than
    # 1 ms (or with equal pts) must not be dropped or reordered.
    assert mediapipe_timestamps_ms([0.0, 0.0334, 0.0667, 0.0667, 0.0669, 1.0]) == [
        0, 33, 67, 68, 69, 1000,
    ]  # fmt: skip
    assert mediapipe_timestamps_ms([]) == []


@pytest.fixture(scope="module")
def ingested(tmp_path_factory: pytest.TempPathFactory) -> FixtureSession:
    session = copy_fixture_session(tmp_path_factory.mktemp("session"))
    ingest(session.root, session.study_id, session.session_id, session.manifest, 32 * 1024 * 1024)
    return session


@pytest.fixture(scope="module")
def landmarks_file(ingested: FixtureSession, face_model: Path) -> Path:
    derived = ingested.root / "studies" / str(ingested.study_id) / "sessions"
    derived = derived / str(ingested.session_id) / "derived"
    detect_landmarks(face_model, ingested.raw / "webcam.webm", derived / LANDMARKS_FILE)
    return derived / LANDMARKS_FILE


def test_every_webcam_frame_gets_a_row(landmarks_file: Path) -> None:
    table = pq.read_table(landmarks_file)
    frames = pq.read_table(landmarks_file.with_name("webcam_frames.parquet"))
    assert table.column_names == ["index", "pts", "face", "lm", "matrix", "blink_l", "blink_r"]
    assert table.num_rows == frames.num_rows > 100
    # The decoded frames have the presentation times that ingest listed from the packets.
    np.testing.assert_allclose(table["pts"].to_numpy(), frames["pts"].to_numpy(), atol=1e-6)
    assert table["index"].to_pylist() == list(range(table.num_rows))


def test_a_face_row_holds_478_landmarks_a_matrix_and_blink_scores(landmarks_file: Path) -> None:
    table = pq.read_table(landmarks_file)
    lm_type, matrix_type = table.schema.field("lm").type, table.schema.field("matrix").type
    assert (lm_type.list_size, lm_type.value_type) == (LANDMARK_COUNT * 3, pa.float32())
    assert (matrix_type.list_size, matrix_type.value_type) == (16, pa.float64())
    face = np.array(table["face"].to_pylist())
    assert face.mean() > 0.9  # the clip shows a face throughout
    lm = np.array([v for v, f in zip(table["lm"].to_pylist(), face, strict=True) if f])
    assert lm.shape[1] == 478 * 3 and lm.dtype == np.float64  # python floats; stored as float32
    xy = lm.reshape(-1, 478, 3)[:, :, :2]
    assert xy.min() >= 0 and xy.max() <= 1  # normalised image coordinates
    matrices = np.array([m for m, f in zip(table["matrix"].to_pylist(), face, strict=True) if f])
    np.testing.assert_allclose(matrices[:, 12:16], [[0, 0, 0, 1]] * len(matrices), atol=1e-3)
    assert (matrices[:, 11] > -100).all() and (matrices[:, 11] < -10).all()  # cm in front of camera
    for column in ("blink_l", "blink_r"):
        values = np.array([v for v, f in zip(table[column].to_pylist(), face, strict=True) if f])
        assert values.min() >= 0 and values.max() <= 1
    # A frame without a face has nothing but its time.
    for row in np.flatnonzero(~face):
        assert table["lm"][int(row)].as_py() is None and table["blink_l"][int(row)].as_py() is None


def test_the_file_remembers_the_frame_size(landmarks_file: Path) -> None:
    metadata = pq.read_schema(landmarks_file).metadata
    assert (int(metadata[b"width"]), int(metadata[b"height"])) == (854, 480)


def test_landmarks_agree_with_the_python_api_run_on_the_face_clip(
    tmp_path: Path, face_model: Path
) -> None:
    """The same clip, decoded by PyAV here and by OpenCV in `scripts/make_face_fixture.py`.

    The two decoders convert colour slightly differently (mean pixel difference about 1/255, at most
    18) and the model's tracking amplifies that to about 0.01 in normalised units (the browser's
    output differs by the same amount, DECISIONS milestone 2), so this checks the same face in the
    same frame, not bit equality. The first frame, found without tracking, agrees to 1e-3.
    """
    out = tmp_path / LANDMARKS_FILE
    detect_landmarks(face_model, FACE_CLIP, out)
    table = pq.read_table(out)
    reference = json.loads(FACE_FRAMES.read_text(encoding="utf-8"))
    assert table.num_rows >= max(f["frame"] for f in reference["frames"]) + 1
    for ref in reference["frames"]:
        row = table.slice(ref["frame"], 1).to_pylist()[0]
        assert row["face"]
        ours = np.array(row["lm"], dtype=float).reshape(478, 3)
        for mesh_index, position in SUBSET_POSITION.items():
            np.testing.assert_allclose(ours[mesh_index], ref["lm"][position], atol=2e-2)
            if ref["frame"] == 0:
                np.testing.assert_allclose(ours[mesh_index], ref["lm"][position], atol=2e-3)
        np.testing.assert_allclose(row["matrix"], ref["matrix"], rtol=0.1, atol=0.1)
        assert row["blink_l"] == pytest.approx(ref["blink_l"], abs=0.1)
        assert row["blink_r"] == pytest.approx(ref["blink_r"], abs=0.1)


def test_a_video_without_a_face_gives_rows_without_landmarks(
    tmp_path: Path, face_model: Path
) -> None:
    clip = tmp_path / "noface.webm"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
         "-c:v", "libvpx", str(clip)],
        check=True,
    )  # fmt: skip
    summary = detect_landmarks(face_model, clip, tmp_path / LANDMARKS_FILE)
    table = pq.read_table(tmp_path / LANDMARKS_FILE)
    assert table.num_rows == summary.frames == 10
    assert summary.frames_with_face == 0
    assert not any(table["face"].to_pylist())
    assert table["lm"].null_count == table["matrix"].null_count == 10


def test_a_missing_model_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(LandmarkError, match="model"):
        detect_landmarks(tmp_path / "nope.task", FACE_CLIP, tmp_path / LANDMARKS_FILE)
    assert not (tmp_path / LANDMARKS_FILE).exists()


def test_a_file_that_is_not_a_video_fails_loudly(tmp_path: Path, face_model: Path) -> None:
    junk = tmp_path / "junk.webm"
    junk.write_bytes(b"\x00" * 100)
    with pytest.raises(LandmarkError, match="cannot be read"):
        detect_landmarks(face_model, junk, tmp_path / LANDMARKS_FILE)
    assert not (tmp_path / LANDMARKS_FILE).exists()
