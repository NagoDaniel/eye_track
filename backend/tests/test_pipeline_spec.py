"""`PipelineSpec`: Pipeline B runs the same fit, prediction, validation and fixation code as A on
its own feature table and writes its own files (PLAN §8.6 to §8.9, DECISIONS 2026-10-20)."""

import json
import shutil
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.storage import derived_dir
from tests.synthetic_session import Synthetic, build
from worker.pipeline.calibration import CALIBRATION_FILE
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURE_VECTOR_COLUMNS, FEATURES_FILE
from worker.pipeline.gaze import FIXATIONS_FILE, GAZE_FILE, run_gaze
from worker.pipeline.predict import predict_session
from worker.pipeline.ridge import RidgeModel
from worker.pipeline.spec import B_FEATURE_COLUMNS, SPEC_A, SPEC_B, spec_of

CONFIG = PipelineConfig()


def test_pipeline_a_keeps_the_names_of_milestones_2_and_3() -> None:
    assert SPEC_A.features_file == FEATURES_FILE == "features_a.parquet"
    assert SPEC_A.calibration_file == CALIBRATION_FILE == "calibration_a.json"
    assert (SPEC_A.gaze_file, SPEC_A.fixations_file) == (GAZE_FILE, FIXATIONS_FILE)
    assert SPEC_A.feature_columns == FEATURE_VECTOR_COLUMNS and len(FEATURE_VECTOR_COLUMNS) == 10


def test_pipeline_b_has_its_own_files_and_six_features() -> None:
    assert SPEC_B.features_file == "features_b.parquet"
    assert (SPEC_B.calibration_file, SPEC_B.gaze_file, SPEC_B.fixations_file) == (
        "calibration_b.json", "gaze_b.parquet", "fixations_b.parquet",
    )  # fmt: skip
    assert SPEC_B.feature_columns == B_FEATURE_COLUMNS
    assert B_FEATURE_COLUMNS == ("gaze_pitch", "gaze_yaw", "yaw", "pitch", "roll", "face_ratio")


def test_spec_of_knows_both_and_refuses_others() -> None:
    assert spec_of("a") is SPEC_A and spec_of("b") is SPEC_B
    with pytest.raises(ValueError, match="unknown pipeline 'c'"):
        spec_of("c")


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Synthetic:
    """A synthetic session whose B features carry the same signal as A's first two features, with
    a calibration model for B that maps them to px, so B must give A's gaze under its own names."""
    s = build(tmp_path_factory.mktemp("spec"))
    derived = derived_dir(s.root, s.study_id, s.session_id)
    a = pq.read_table(derived / FEATURES_FILE)
    columns = {
        "gaze_pitch": a["iris_l_x"], "gaze_yaw": a["iris_l_y"], "yaw": a["yaw"],
        "pitch": a["pitch"], "roll": a["roll"], "face_ratio": a["face_ratio"],
    }  # fmt: skip
    table = pa.table({"index": a["index"], "pts": a["pts"], "face": a["face"], **columns,
                      "blink": a["blink"]})  # fmt: skip
    pq.write_table(table, derived / SPEC_B.features_file)
    calibration = json.loads((derived / SPEC_A.calibration_file).read_text())
    calibration["feature_columns"] = list(B_FEATURE_COLUMNS)
    weights = np.zeros((6, 2))  # degree 1: x = 1000 * gaze_pitch, y = 1000 * gaze_yaw
    weights[0, 0], weights[1, 1] = 1000.0, 1000.0
    calibration["model"] = RidgeModel(
        1.0, 1, np.zeros(6), np.ones(6), weights, np.zeros(2)
    ).as_dict()
    (derived / SPEC_B.calibration_file).write_text(json.dumps(calibration))
    return s


def test_b_predicts_from_its_own_table_and_calibration(session: Synthetic) -> None:
    a = predict_session(session.root, session.study_id, session.session_id, CONFIG, SPEC_A)
    b = predict_session(session.root, session.study_id, session.session_id, CONFIG, SPEC_B)
    np.testing.assert_allclose(a.gaze.x_px, b.gaze.x_px, equal_nan=True)
    assert (a.gaze.valid == b.gaze.valid).all()


def test_a_calibration_of_other_features_is_refused(session: Synthetic, tmp_path: Path) -> None:
    root = tmp_path / "data"
    shutil.copytree(session.root, root)
    derived = derived_dir(root, session.study_id, session.session_id)
    path = derived / SPEC_B.calibration_file
    wrong = json.loads(path.read_text())
    wrong["feature_columns"] = list(FEATURE_VECTOR_COLUMNS)
    path.write_text(json.dumps(wrong))
    with pytest.raises(ValueError, match="other features"):
        predict_session(root, session.study_id, session.session_id, CONFIG, SPEC_B)


def test_b_writes_its_own_gaze_files_and_leaves_a_alone(session: Synthetic, tmp_path: Path) -> None:
    root = tmp_path / "data"
    shutil.copytree(session.root, root)
    derived = derived_dir(root, session.study_id, session.session_id)
    run_gaze(root, session.study_id, session.session_id, CONFIG, SPEC_A)
    a_bytes = (derived / SPEC_A.gaze_file).read_bytes()
    summary = run_gaze(root, session.study_id, session.session_id, CONFIG, SPEC_B)

    assert (derived / SPEC_B.gaze_file).is_file() and (derived / SPEC_B.fixations_file).is_file()
    assert (derived / SPEC_A.gaze_file).read_bytes() == a_bytes  # untouched
    a_table, b_table = (pq.read_table(derived / n) for n in (SPEC_A.gaze_file, SPEC_B.gaze_file))
    assert a_table.column_names == b_table.column_names
    for name in a_table.column_names:  # same features in, same gaze out
        a_col, b_col = a_table[name], b_table[name]
        if pa.types.is_floating(a_col.type):
            np.testing.assert_allclose(
                a_col.to_numpy(zero_copy_only=False),
                b_col.to_numpy(zero_copy_only=False),
                atol=1e-3,
                equal_nan=True,
                err_msg=name,
            )
        else:
            assert a_col.equals(b_col), name
    assert json.loads(a_table.schema.metadata[b"gaze"])["pipeline"] == "a"
    assert json.loads(b_table.schema.metadata[b"gaze"])["pipeline"] == "b"
    assert summary.fixations == pq.read_table(derived / SPEC_B.fixations_file).num_rows > 0
