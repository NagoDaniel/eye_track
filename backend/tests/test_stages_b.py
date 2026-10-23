"""The calibration and gaze stages of Pipeline B on the recorded session (PLAN §8.6 to §8.9).

B's features here come from the table of the real session (head pose and blinks from A, real
landmarks and video); the stand-in for the network gives them an eye signal, so these tests are
about the plumbing: the same fit and prediction code, B's own files, A's schemas."""

import json
import shutil
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from app.storage import derived_dir
from tests.analysis_data import process_fixture
from tests.session_fixture import FixtureSession
from tests.test_features_b import BrightnessEstimator, paths
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_b import compute_features_b
from worker.pipeline.model_b import GazeModelB
from worker.pipeline.runner import Context, calibration_stage, calibration_status, gaze_stage
from worker.pipeline.spec import SPEC_A, SPEC_B

CONFIG = PipelineConfig()


@pytest.fixture(scope="module")
def processed(tmp_path_factory: pytest.TempPathFactory, face_model: Path) -> FixtureSession:
    return process_fixture(tmp_path_factory.mktemp("stages_b"), face_model)


@pytest.fixture
def with_features_b(processed: FixtureSession, tmp_path: Path) -> Context:
    """A copy of the processed session with `features_b.parquet` added."""
    shutil.copytree(processed.root, tmp_path / "data")
    root = tmp_path / "data"
    video, landmarks, features_a = (root / p.relative_to(processed.root) for p in paths(processed))
    out = derived_dir(root, processed.study_id, processed.session_id) / SPEC_B.features_file
    compute_features_b(video, landmarks, features_a, out, BrightnessEstimator(), {}, CONFIG)
    return Context(
        root=root, study_id=processed.study_id, session_id=processed.session_id, config=CONFIG,
        model=Path("unused"), max_inflated_bytes=1,
    )  # fmt: skip


def test_calibration_b_is_fitted_on_b_features_and_written_apart(with_features_b: Context) -> None:
    ctx = with_features_b
    a_before = (ctx.derived / SPEC_A.calibration_file).read_bytes()
    summary = calibration_stage(ctx, SPEC_B)
    written = json.loads((ctx.derived / SPEC_B.calibration_file).read_text())
    assert written["feature_columns"] == list(SPEC_B.feature_columns)
    assert written["status"] == summary["status"] == calibration_status(ctx.derived, SPEC_B)
    assert (ctx.derived / SPEC_A.calibration_file).read_bytes() == a_before  # A is untouched
    assert json.loads(a_before)["feature_columns"] == list(SPEC_A.feature_columns)


def test_gaze_b_has_the_schema_of_gaze_a_and_leaves_a_alone(with_features_b: Context) -> None:
    ctx = with_features_b
    a_gaze = (ctx.derived / SPEC_A.gaze_file).read_bytes()
    a_fix = (ctx.derived / SPEC_A.fixations_file).read_bytes()
    summary = calibration_stage(ctx, SPEC_B)
    result = gaze_stage(ctx, SPEC_B)
    assert (ctx.derived / SPEC_A.gaze_file).read_bytes() == a_gaze
    assert (ctx.derived / SPEC_A.fixations_file).read_bytes() == a_fix
    if summary["status"] != "ok":  # the stand-in's signal may not calibrate: then B is skipped
        assert result == {"skipped": "calibration_failed"}
        assert not (ctx.derived / SPEC_B.gaze_file).exists()
        return
    assert result["frames"] > 0
    a, b = (pq.read_table(ctx.derived / n) for n in (SPEC_A.gaze_file, SPEC_B.gaze_file))
    assert b.schema.names == a.schema.names and b.schema.types == a.schema.types
    fa, fb = (
        pq.read_table(ctx.derived / n) for n in (SPEC_A.fixations_file, SPEC_B.fixations_file)
    )
    assert fb.schema.names == fa.schema.names and fb.schema.types == fa.schema.types
    assert b.num_rows == a.num_rows


def test_a_failed_calibration_skips_gaze_b_and_removes_its_old_files(
    with_features_b: Context,
) -> None:
    ctx = with_features_b
    for name in (SPEC_B.gaze_file, SPEC_B.fixations_file):
        (ctx.derived / name).write_bytes(b"stale")
    failed = json.loads((ctx.derived / SPEC_A.calibration_file).read_text())
    failed.update(status="failed", failed_reason="no_calibration_dots")
    (ctx.derived / SPEC_B.calibration_file).write_text(json.dumps(failed))
    assert calibration_status(ctx.derived, SPEC_B) == "failed"
    assert gaze_stage(ctx, SPEC_B) == {"skipped": "calibration_failed"}
    assert not (ctx.derived / SPEC_B.gaze_file).exists()
    assert not (ctx.derived / SPEC_B.fixations_file).exists()
    assert (ctx.derived / SPEC_A.gaze_file).exists()  # A keeps its files


def test_without_a_calibration_file_b_is_not_ok(with_features_b: Context) -> None:
    assert calibration_status(with_features_b.derived, SPEC_B) is None


# --- the real network ---


def test_the_whole_chain_with_the_real_network(
    processed: FixtureSession, model_b_root: Path, tmp_path: Path
) -> None:
    """Real MediaPipe landmarks, the real gaze network, the same fit and prediction as A: B's
    files exist with A's schema (or B is skipped because this session's calibration failed)."""
    shutil.copytree(processed.root, tmp_path / "data")
    root = tmp_path / "data"
    video, landmarks, features_a = (root / p.relative_to(processed.root) for p in paths(processed))
    derived = derived_dir(root, processed.study_id, processed.session_id)
    model = GazeModelB.load(model_b_root, "l2cs_mobileone_s0")
    compute_features_b(video, landmarks, features_a, derived / SPEC_B.features_file, model,
                       {"name": model.name}, CONFIG)  # fmt: skip
    ctx = Context(root=root, study_id=processed.study_id, session_id=processed.session_id,
                  config=CONFIG, model=Path("unused"), max_inflated_bytes=1)  # fmt: skip

    calibration = calibration_stage(ctx, SPEC_B)
    gaze = gaze_stage(ctx, SPEC_B)
    written = json.loads((derived / SPEC_B.calibration_file).read_text())
    assert written["feature_columns"] == list(SPEC_B.feature_columns)
    if calibration["status"] == "ok":
        assert calibration["cv_error_px"] is not None and gaze["frames"] > 0
        a = pq.read_table(derived / SPEC_A.gaze_file)
        b = pq.read_table(derived / SPEC_B.gaze_file)
        assert b.schema.names == a.schema.names and b.num_rows == a.num_rows
        assert (derived / SPEC_B.fixations_file).exists()
    else:
        assert gaze == {"skipped": "calibration_failed"}
