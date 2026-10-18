"""Pipeline A feature table (PLAN §8.4) from server-side landmarks, with the real MediaPipe."""

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import (
    FEATURES_FILE,
    SUBSET_POSITION,
    compute_features,
    is_blink,
)
from worker.pipeline.landmarks import LANDMARK_COUNT, MATRIX_SIZE, SCHEMA_FIELDS, detect_landmarks

REPO = Path(__file__).resolve().parents[2]
FACE_CLIP = REPO / "e2e" / "fixtures" / "face.mp4"
FACE_FRAMES = json.loads((REPO / "shared" / "fixtures" / "face_frames.json").read_text("utf-8"))
CONFIG = PipelineConfig()
WIDTH, HEIGHT = 854, 480


def test_blink_rule_is_the_plans() -> None:
    assert not is_blink(0.1, 0.1, 0.3, 0.3, CONFIG)
    assert is_blink(0.51, 0.1, 0.3, 0.3, CONFIG)  # either eye's blendshape above 0.5
    assert is_blink(0.1, 0.51, 0.3, 0.3, CONFIG)
    assert is_blink(0.1, 0.1, 0.14, 0.3, CONFIG)  # or either eye's openness below the threshold
    assert is_blink(0.1, 0.1, 0.3, 0.14, CONFIG)
    # The limits themselves are not blinks (strict comparisons, as in the browser).
    assert not is_blink(0.5, 0.5, 0.15, 0.15, CONFIG)


def test_blink_thresholds_come_from_the_config() -> None:
    strict = replace(CONFIG, blink_score_threshold=0.2, blink_openness_threshold=0.3)
    assert is_blink(0.3, 0.0, 0.5, 0.5, strict)
    assert is_blink(0.0, 0.0, 0.25, 0.5, strict)
    assert not is_blink(0.3, 0.0, 0.5, 0.5, CONFIG)


def write_landmarks(path: Path, frames: list[dict[str, object] | None]) -> None:
    """A landmarks.parquet whose faces are the 49-landmark frames of the real-MediaPipe fixture,
    placed at their mesh indices (the other landmarks are zero). `None`: a frame without a face."""
    n = len(frames)
    lm = np.zeros((n, LANDMARK_COUNT * 3), dtype=np.float32)
    matrix = np.zeros((n, MATRIX_SIZE))
    blink = np.zeros((n, 2), dtype=np.float32)
    for i, frame in enumerate(frames):
        if frame is None:
            continue
        full = lm[i].reshape(LANDMARK_COUNT, 3)
        for mesh_index, position in SUBSET_POSITION.items():
            full[mesh_index] = frame["lm"][position]  # type: ignore[index]
        matrix[i] = np.asarray(frame["matrix"], dtype=float)
        blink[i] = (frame["blink_l"], frame["blink_r"])  # type: ignore[assignment]
    no_face = np.array([f is None for f in frames], dtype=np.bool_)
    table = pa.Table.from_arrays(
        [
            pa.array(range(n), pa.int64()),
            pa.array([i / 30 for i in range(n)], pa.float64()),
            pa.array([f is not None for f in frames]),
            pa.FixedSizeListArray.from_arrays(
                pa.array(lm.ravel()), LANDMARK_COUNT * 3, mask=pa.array(no_face)
            ),
            pa.FixedSizeListArray.from_arrays(
                pa.array(matrix.ravel()), MATRIX_SIZE, mask=pa.array(no_face)
            ),
            pa.array(blink[:, 0], mask=no_face),
            pa.array(blink[:, 1], mask=no_face),
        ],
        schema=pa.schema(SCHEMA_FIELDS).with_metadata({"width": str(WIDTH), "height": str(HEIGHT)}),
    )
    pq.write_table(table, path)


def test_the_table_has_the_features_of_the_fixture_frames(tmp_path: Path) -> None:
    """Landmarks at their mesh indices give exactly the features the shared fixture lists."""
    frames: list[dict[str, object] | None] = [*FACE_FRAMES["frames"][:5]]
    frames.insert(2, None)  # a frame without a face in between
    write_landmarks(tmp_path / "landmarks.parquet", frames)
    # The fixture's aspect ratio is the clip's; the stored frame size gives the same one.
    assert pytest.approx(FACE_FRAMES["aspect"], rel=1e-3) == WIDTH / HEIGHT

    summary = compute_features(tmp_path / "landmarks.parquet", tmp_path / FEATURES_FILE, CONFIG)

    rows = pq.read_table(tmp_path / FEATURES_FILE).to_pylist()
    assert (summary.frames, summary.frames_with_face) == (6, 5)
    assert rows[2]["face"] is False and rows[2]["open_l"] is None and rows[2]["blink"] is False
    faces = [r for r in rows if r["face"]]
    for row, ref in zip(faces, FACE_FRAMES["frames"][:5], strict=True):
        exp = ref["expected"]
        tolerance = 5e-4  # float32 landmarks; the aspect ratio is the rounded 854 / 480
        assert [row["iris_l_x"], row["iris_l_y"]] == pytest.approx(exp["iris_l"], abs=tolerance)
        assert [row["iris_r_x"], row["iris_r_y"]] == pytest.approx(exp["iris_r"], abs=tolerance)
        assert (row["open_l"], row["open_r"]) == pytest.approx(
            (exp["open_l"], exp["open_r"]), abs=tolerance
        )
        assert (row["yaw"], row["pitch"], row["roll"]) == pytest.approx(
            (exp["yaw"], exp["pitch"], exp["roll"]), abs=0.05
        )
        assert (row["tx"], row["ty"], row["tz"]) == pytest.approx(
            (exp["tx"], exp["ty"], exp["tz"]), abs=1e-4
        )
        assert row["face_ratio"] == pytest.approx(exp["face_ratio"], abs=tolerance)
        assert (row["blink_l"], row["blink_r"]) == pytest.approx(
            (ref["blink_l"], ref["blink_r"]), abs=1e-6
        )


def test_the_file_records_the_thresholds_it_used(tmp_path: Path) -> None:
    write_landmarks(tmp_path / "landmarks.parquet", [FACE_FRAMES["frames"][0]])
    custom = replace(CONFIG, blink_openness_threshold=0.4)
    summary = compute_features(tmp_path / "landmarks.parquet", tmp_path / FEATURES_FILE, custom)
    metadata = pq.read_schema(tmp_path / FEATURES_FILE).metadata
    assert json.loads(metadata[b"config"]) == custom.as_dict()
    # An open eye (openness 0.28) is a blink under that threshold.
    assert summary.blinks == 1


def test_a_file_without_a_frame_size_fails_loudly(tmp_path: Path) -> None:
    write_landmarks(tmp_path / "landmarks.parquet", [FACE_FRAMES["frames"][0]])
    table = pq.read_table(tmp_path / "landmarks.parquet").replace_schema_metadata({})
    pq.write_table(table, tmp_path / "landmarks.parquet")
    with pytest.raises(ValueError, match="width"):
        compute_features(tmp_path / "landmarks.parquet", tmp_path / FEATURES_FILE, CONFIG)


def test_features_of_the_face_clip_found_by_real_mediapipe(
    tmp_path: Path, face_model: Path
) -> None:
    detect_landmarks(face_model, FACE_CLIP, tmp_path / "landmarks.parquet")
    summary = compute_features(tmp_path / "landmarks.parquet", tmp_path / FEATURES_FILE, CONFIG)
    table = pq.read_table(tmp_path / FEATURES_FILE)
    assert table.num_rows == summary.frames > 300
    assert table.column_names == [
        "index", "pts", "face", "iris_l_x", "iris_l_y", "iris_r_x", "iris_r_y", "open_l", "open_r",
        "yaw", "pitch", "roll", "tx", "ty", "tz", "face_ratio", "blink_l", "blink_r", "blink",
    ]  # fmt: skip
    rows = [r for r in table.to_pylist() if r["face"]]
    assert len(rows) / table.num_rows > 0.95

    # The clip has blinks (openness 0.07 to 0.14 against 0.27 to 0.39 with open eyes) and they are
    # found: a few percent of the frames, and every one is explained by the rule.
    blinks = [r for r in rows if r["blink"]]
    assert 0.005 < len(blinks) / len(rows) < 0.15
    for r in rows:
        assert r["blink"] == is_blink(r["blink_l"], r["blink_r"], r["open_l"], r["open_r"], CONFIG)
    open_eyes = [r["open_l"] for r in rows if not r["blink"]]
    assert 0.2 < float(np.median(open_eyes)) < 0.45

    # Against the features that `scripts/make_face_fixture.py` computed from OpenCV-decoded frames:
    # the same head pose and eye state, within what the two decoders' colour conversions change.
    for ref in FACE_FRAMES["frames"]:
        row = table.slice(ref["frame"], 1).to_pylist()[0]
        exp = ref["expected"]
        assert (row["yaw"], row["pitch"], row["roll"]) == pytest.approx(
            (exp["yaw"], exp["pitch"], exp["roll"]), abs=4
        )
        assert (row["open_l"], row["open_r"]) == pytest.approx(
            (exp["open_l"], exp["open_r"]), abs=0.1
        )
        assert row["face_ratio"] == pytest.approx(exp["face_ratio"], abs=0.005)


def test_server_features_match_the_browsers_on_the_recorded_session(
    tmp_path: Path, face_model: Path
) -> None:
    """Same recording, two pipelines: the browser's live model and the server's, per PLAN §8.2's
    premise that they see the same face. Compared as medians because the frames are not paired
    yet (that is the sync stage)."""
    from tests.session_fixture import copy_fixture_session
    from worker.pipeline.features_a import frame_features
    from worker.pipeline.ingest import ingest

    session = copy_fixture_session(tmp_path)
    ingest(session.root, session.study_id, session.session_id, session.manifest, 32 * 1024 * 1024)
    derived = session.raw.parent / "derived"
    detect_landmarks(face_model, session.raw / "webcam.webm", derived / "landmarks.parquet")
    compute_features(derived / "landmarks.parquet", derived / FEATURES_FILE, CONFIG)
    server = [r for r in pq.read_table(derived / FEATURES_FILE).to_pylist() if r["face"]]

    browser = [
        json.loads(line)
        for line in (session.raw / "browser_features.jsonl").read_text("utf-8").splitlines()
    ]
    aspect = WIDTH / HEIGHT
    ours = [frame_features(r["lm"], r["matrix"], aspect) for r in browser if r["face"]]

    def median(values: list[float]) -> float:
        return float(np.median(values))

    # The server looks at every frame, the browser at the frames its model could keep up with.
    assert len(server) >= 0.9 * len(ours)
    assert median([r["face_ratio"] for r in server]) == pytest.approx(
        median([f.face_ratio for f in ours]), abs=0.003
    )
    assert median([r["yaw"] for r in server]) == pytest.approx(median([f.yaw for f in ours]), abs=4)
    assert median([r["pitch"] for r in server]) == pytest.approx(
        median([f.pitch for f in ours]), abs=4
    )
    assert median([r["open_l"] for r in server]) == pytest.approx(
        median([f.open_l for f in ours]), abs=0.05
    )
