"""Pipeline B features (PLAN §8.5): the face crop and the table, on the recorded session."""

import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
import pytest

from app.storage import derived_dir, webcam_path
from tests.analysis_data import process_fixture
from tests.session_fixture import FixtureSession
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURES_FILE
from worker.pipeline.features_b import (
    FEATURES_B_FILE,
    HEAD_COLUMNS,
    INPUT_SIZE,
    FeaturesBError,
    FeaturesBSummary,
    Floats,
    GazeEstimator,
    Image,
    compute_features_b,
    crop_square,
    face_boxes,
)
from worker.pipeline.landmarks import LANDMARKS_FILE
from worker.pipeline.model_b import GazeModelB
from worker.pipeline.spec import B_FEATURE_COLUMNS

CONFIG = PipelineConfig()


def landmarks_in_box(x0: float, y0: float, x1: float, y1: float) -> np.ndarray:
    """One face whose 478 landmarks fill the box (normalised image coordinates)."""
    lm = np.zeros((1, 478, 3), dtype=np.float32)
    lm[0, :, 0] = np.linspace(x0, x1, 478)
    lm[0, :, 1] = np.linspace(y0, y1, 478)
    return lm


def test_the_crop_is_a_square_around_the_centre_of_the_landmark_box() -> None:
    # a face 200 x 100 px in a 1000 x 500 frame: the square follows the longer side
    boxes = face_boxes(landmarks_in_box(0.4, 0.4, 0.6, 0.6), 1000, 500, 1.2)
    cx, cy, side = boxes[0]
    assert (cx, cy) == pytest.approx((500.0, 250.0))
    assert side == pytest.approx(200.0 * 1.2)
    tall = face_boxes(landmarks_in_box(0.45, 0.2, 0.55, 0.8), 1000, 500, 1.2)
    assert tall[0][2] == pytest.approx(300.0 * 1.2)  # the box is taller than wide


def test_the_expansion_is_the_configured_one() -> None:
    lm = landmarks_in_box(0.4, 0.4, 0.6, 0.6)
    assert face_boxes(lm, 1000, 1000, 1.0)[0][2] == pytest.approx(200.0)
    assert face_boxes(lm, 1000, 1000, 1.5)[0][2] == pytest.approx(300.0)
    assert CONFIG.b_crop_scale == 1.2  # the plan's "expanded 20 %"


def test_crop_square_scales_and_centres() -> None:
    frame = np.zeros((300, 400, 3), dtype=np.uint8)
    frame[100:200, 150:250] = (255, 0, 0)  # a 100 px red square centred at (200, 150)
    crop = crop_square(frame, 200.0, 150.0, 200.0)  # twice its size
    assert crop.shape == (INPUT_SIZE, INPUT_SIZE, 3)
    red = (crop[:, :, 0] > 200) & (crop[:, :, 1] < 50)
    ys, xs = np.nonzero(red)
    assert abs((xs.min() + xs.max()) / 2 - INPUT_SIZE / 2) < 2
    assert abs((ys.min() + ys.max()) / 2 - INPUT_SIZE / 2) < 2
    assert abs((xs.max() - xs.min()) - INPUT_SIZE / 2) < 3  # half the crop's width


def test_crop_square_is_black_outside_the_frame() -> None:
    frame = np.full((100, 100, 3), 200, dtype=np.uint8)
    crop = crop_square(frame, 0.0, 0.0, 100.0)  # the top left quarter is outside the frame
    assert crop[10, 10].tolist() == [0, 0, 0]
    assert crop[-10, -10].tolist() == [200, 200, 200]


class BrightnessEstimator:
    """A deterministic stand-in for the network, only for the checks of the table's plumbing:
    its 'angles' are the mean red and green of the crop."""

    def predict(self, images: Sequence[Image], batch_size: int = 8) -> tuple[Floats, Floats]:
        arr = np.stack([np.asarray(i, dtype=np.float64) for i in images])
        return arr[:, :, :, 0].mean(axis=(1, 2)) / 255.0, arr[:, :, :, 1].mean(axis=(1, 2)) / 255.0


@pytest.fixture(scope="module")
def processed(tmp_path_factory: pytest.TempPathFactory, face_model: Path) -> FixtureSession:
    return process_fixture(tmp_path_factory.mktemp("features_b"), face_model)


def paths(s: FixtureSession) -> tuple[Path, Path, Path]:
    derived = derived_dir(s.root, s.study_id, s.session_id)
    return (
        webcam_path(s.root, s.study_id, s.session_id),
        derived / LANDMARKS_FILE,
        (derived / FEATURES_FILE),
    )


def run(s: FixtureSession, out: Path, estimator: GazeEstimator | None = None) -> FeaturesBSummary:
    video, landmarks, features_a = paths(s)
    return compute_features_b(
        video, landmarks, features_a, out, estimator or BrightnessEstimator(),
        {"name": "stand-in"}, CONFIG,
    )  # fmt: skip


def test_the_table_has_a_row_per_frame_and_a_s_head_pose(
    processed: FixtureSession, tmp_path: Path
) -> None:
    summary = run(processed, tmp_path / FEATURES_B_FILE)
    _, landmarks, features_a = paths(processed)
    b, a, marks = (pq.read_table(p) for p in (tmp_path / FEATURES_B_FILE, features_a, landmarks))
    assert b.num_rows == a.num_rows == marks.num_rows == summary.frames
    assert b.column_names == [
        "index", "pts", "face", "gaze_pitch", "gaze_yaw", *HEAD_COLUMNS, "blink",
    ]  # fmt: skip
    for name in ("index", "pts", "face", *HEAD_COLUMNS, "blink"):
        assert b[name].equals(a[name]), name  # copied from A
    assert set(B_FEATURE_COLUMNS) <= set(b.column_names)
    face = np.asarray(b["face"].to_pylist(), dtype=bool)
    pitch = b["gaze_pitch"].to_numpy(zero_copy_only=False)
    assert face.sum() == summary.frames_with_face > 0
    assert np.isfinite(pitch[face]).all()
    assert np.isnan(pitch[~face]).all()  # null where no face was found


def test_the_crops_follow_the_face(processed: FixtureSession, tmp_path: Path) -> None:
    """The stand-in's output is the crop's brightness: it changes when the face moves in the frame
    (a crop that stayed fixed would give a constant per-frame brightness pattern)."""
    run(processed, tmp_path / FEATURES_B_FILE)
    b = pq.read_table(tmp_path / FEATURES_B_FILE)
    face = np.asarray(b["face"].to_pylist(), dtype=bool)
    red = b["gaze_pitch"].to_numpy(zero_copy_only=False)[face]
    assert red.std() > 0 and 0.0 < red.mean() < 1.0
    meta = json.loads(pq.read_schema(tmp_path / FEATURES_B_FILE).metadata[b"features_b"])
    assert (
        meta["crop_scale"] == 1.2
        and meta["input_size"] == 448
        and meta["model"] == {"name": "stand-in"}
    )


def test_a_rerun_gives_the_same_file(processed: FixtureSession, tmp_path: Path) -> None:
    run(processed, tmp_path / "one.parquet")
    run(processed, tmp_path / "two.parquet")
    assert pq.read_table(tmp_path / "one.parquet").equals(pq.read_table(tmp_path / "two.parquet"))


def test_inputs_that_do_not_fit_together_fail_loudly(
    processed: FixtureSession, tmp_path: Path
) -> None:
    video, landmarks, features_a = paths(processed)
    short = tmp_path / "short.parquet"
    pq.write_table(pq.read_table(features_a).slice(0, 10), short)
    with pytest.raises(FeaturesBError, match="features_a has 10 rows"):
        compute_features_b(video, landmarks, short, tmp_path / "o.parquet",
                           BrightnessEstimator(), {}, CONFIG)  # fmt: skip
    other = tmp_path / "other.webm"
    shutil.copy(video, other)
    cut = pq.read_table(landmarks).slice(0, 20)
    cut_path = tmp_path / "cut.parquet"
    pq.write_table(cut.replace_schema_metadata(pq.read_schema(landmarks).metadata), cut_path)
    cut_a = tmp_path / "cut_a.parquet"
    pq.write_table(pq.read_table(features_a).slice(0, 20), cut_a)
    with pytest.raises(FeaturesBError, match="more frames than landmarks"):
        compute_features_b(other, cut_path, cut_a, tmp_path / "o2.parquet",
                           BrightnessEstimator(), {}, CONFIG)  # fmt: skip


# --- the real network (MobileOne weights) on the real session ---


@pytest.fixture(scope="module")
def real_model(model_b_root: Path) -> GazeModelB:
    return GazeModelB.load(model_b_root, "l2cs_mobileone_s0")


def run_real(s: FixtureSession, out: Path, model: GazeModelB) -> FeaturesBSummary:
    video, landmarks, features_a = paths(s)
    info = {"name": model.name, "sha256": model.sha256}
    return compute_features_b(video, landmarks, features_a, out, model, info, CONFIG)


def test_the_real_network_gives_plausible_angles_on_the_recorded_face(
    processed: FixtureSession, real_model: GazeModelB, tmp_path: Path
) -> None:
    summary = run_real(processed, tmp_path / FEATURES_B_FILE, real_model)
    b = pq.read_table(tmp_path / FEATURES_B_FILE)
    face = np.asarray(b["face"].to_pylist(), dtype=bool)
    pitch = b["gaze_pitch"].to_numpy(zero_copy_only=False)
    yaw = b["gaze_yaw"].to_numpy(zero_copy_only=False)
    assert summary.frames == b.num_rows and summary.frames_with_face == int(face.sum()) > 100
    assert np.isfinite(pitch[face]).all() and np.isfinite(yaw[face]).all()
    assert np.isnan(pitch[~face]).all() and np.isnan(yaw[~face]).all()
    # a face looking at the screen: nowhere near the limits of the network's range (about 3.1 rad)
    assert np.abs(pitch[face]).max() < 0.8 and np.abs(yaw[face]).max() < 0.8
    assert pitch[face].std() > 0 and yaw[face].std() > 0  # the angles move with the face and eyes
    assert summary.seconds_per_face is not None and summary.seconds_per_face > 0
    meta = json.loads(pq.read_schema(tmp_path / FEATURES_B_FILE).metadata[b"features_b"])
    assert meta["model"]["name"] == "l2cs_mobileone_s0" and len(meta["model"]["sha256"]) == 64


def test_the_real_network_gives_the_same_file_twice(
    processed: FixtureSession, real_model: GazeModelB, tmp_path: Path
) -> None:
    run_real(processed, tmp_path / "one.parquet", real_model)
    run_real(processed, tmp_path / "two.parquet", real_model)
    assert pq.read_table(tmp_path / "one.parquet").equals(pq.read_table(tmp_path / "two.parquet"))
