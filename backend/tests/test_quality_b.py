"""The quality report of Pipeline B and its inclusion decision (PLAN §8.10, §4.1), on the recorded
session with the real small gaze network (the fixture `b_session`)."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.pipeline_b import QUALITY_B_FILE
from app.storage import derived_dir, meta_path
from tests.session_fixture import FixtureSession
from worker.pipeline.config import PipelineConfig
from worker.pipeline.quality_b import build_quality_b
from worker.pipeline.spec import SPEC_A, SPEC_B

CONFIG = PipelineConfig()


def copy_of(s: FixtureSession, tmp_path: Path) -> FixtureSession:
    root = tmp_path / "data"
    shutil.copytree(s.root, root)
    return FixtureSession(
        root, s.study_id, s.session_id, s.manifest, root / s.raw.relative_to(s.root)
    )


def report_of(s: FixtureSession) -> dict[str, Any]:
    return build_quality_b(s.root, s.study_id, s.session_id, CONFIG, {"features_b": {"seconds": 1}})


def test_the_report_has_every_field_and_is_plain_json(b_session: FixtureSession) -> None:
    report = report_of(b_session)
    derived = derived_dir(b_session.root, b_session.study_id, b_session.session_id)
    assert json.loads((derived / QUALITY_B_FILE).read_text()) == report
    assert set(report) == {
        "version", "pipeline", "config", "is_debug", "features", "calibration", "playback",
        "validation", "inclusion", "stages",
    }  # fmt: skip
    assert report["pipeline"] == "b" and report["stages"] == {"features_b": {"seconds": 1}}
    features = report["features"]
    assert (
        features["model"]["name"] == "l2cs_mobileone_s0" and len(features["model"]["sha256"]) == 64
    )
    assert features["frames_with_face"] == features["frames_with_angles"] > 100
    assert features["crop_scale"] == 1.2 and features["input_size"] == 448
    calibration = report["calibration"]
    assert calibration["status"] == "ok"
    assert calibration["feature_columns"] == list(SPEC_B.feature_columns)
    assert calibration["cv"]["error_px"] > 0 and calibration["samples"]
    playback = report["playback"]
    assert set(playback) == {"frames", "valid_frames", "valid_fraction", "blink_fraction",
                             "interpolated_fraction", "per_video"}  # fmt: skip
    validation = report["validation"]
    assert validation["blocks"] and validation["mean_error_norm"] > 0
    assert set(validation) >= {"blocks", "drift", "precision_px", "mean_error_corrected_norm"}
    assert set(report["inclusion"]) == {"included", "reasons", "criteria"}


def test_inclusion_follows_the_studys_thresholds(b_session: FixtureSession, tmp_path: Path) -> None:
    s = copy_of(b_session, tmp_path)
    loose = report_of(s)["inclusion"]
    assert loose["included"] is True and loose["reasons"] == []  # `process_fixture` loosened them

    path = meta_path(s.root, s.study_id, s.session_id)
    meta = json.loads(path.read_text())
    meta["config"]["inclusion"] = {"max_validation_error_norm": 0.001, "min_valid_fraction": 0.99}
    path.write_text(json.dumps(meta))
    strict = report_of(s)["inclusion"]
    assert strict["included"] is False
    assert set(strict["reasons"]) == {"validation_error_high", "valid_fraction_low"}
    assert strict["criteria"]["max_validation_error_norm"] == 0.001


def test_a_debug_session_is_not_included(b_session: FixtureSession, tmp_path: Path) -> None:
    s = copy_of(b_session, tmp_path)
    path = meta_path(s.root, s.study_id, s.session_id)
    meta = json.loads(path.read_text())
    meta["is_debug"] = True
    path.write_text(json.dumps(meta))
    inclusion = report_of(s)["inclusion"]
    assert inclusion["included"] is False and "debug_session" in inclusion["reasons"]


def test_a_failed_calibration_excludes_without_gaze(
    b_session: FixtureSession, tmp_path: Path
) -> None:
    s = copy_of(b_session, tmp_path)
    derived = derived_dir(s.root, s.study_id, s.session_id)
    failed = json.loads((derived / SPEC_B.calibration_file).read_text())
    failed.update(status="failed", failed_reason="no_calibration_dots")
    (derived / SPEC_B.calibration_file).write_text(json.dumps(failed))
    (derived / SPEC_B.gaze_file).unlink()
    report = report_of(s)
    assert report["playback"] is None and report["validation"] is None
    assert report["inclusion"]["included"] is False
    assert "calibration_failed" in report["inclusion"]["reasons"]


def test_building_the_report_leaves_a_alone(b_session: FixtureSession, tmp_path: Path) -> None:
    s = copy_of(b_session, tmp_path)
    derived = derived_dir(s.root, s.study_id, s.session_id)
    before = {n: (derived / n).read_bytes() for n in ("quality.json", SPEC_A.gaze_file)}
    report_of(s)
    for name, data in before.items():
        assert (derived / name).read_bytes() == data, name


def test_without_the_calibration_file_it_fails_loudly(
    b_session: FixtureSession, tmp_path: Path
) -> None:
    s = copy_of(b_session, tmp_path)
    (derived_dir(s.root, s.study_id, s.session_id) / SPEC_B.calibration_file).unlink()
    with pytest.raises(FileNotFoundError):
        report_of(s)
