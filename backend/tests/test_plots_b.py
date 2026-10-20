"""The debug plots of Pipeline B (PLAN §12.2): the same drawing code on B's files, on the recorded
session after A and B (the real small gaze network). No database."""

import json
import shutil
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pyarrow.parquet as pq
import pytest
from matplotlib.figure import Figure

import worker.pipeline.plots as plots
from app.api.admin_sessions import debug_file
from app.pipeline_b import B_STAGE_DESCRIPTIONS, B_STAGE_NAMES, QUALITY_B_FILE, STAGES_B_FILE
from app.pipeline_names import PLOT_B_CAPTIONS, PLOT_B_FILES, PLOT_CAPTIONS, PLOT_FILES
from app.service_errors import NotFoundError
from app.stage_status import status_of
from tests.session_fixture import FixtureSession
from worker.pipeline.config import PipelineConfig
from worker.pipeline.plots import DEBUG_DIR, load_plot_data, run_plots
from worker.pipeline.runner import Context
from worker.pipeline.runner_b import STAGES_B, run_pipeline_b
from worker.pipeline.spec import SPEC_A, SPEC_B

CONFIG = PipelineConfig()
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def copy(b_session: FixtureSession, tmp_path: Path) -> Context:
    """A copy of the session after A and B (the original is shared and read-only)."""
    shutil.copytree(b_session.root, tmp_path / "data")
    return Context(
        root=tmp_path / "data", study_id=b_session.study_id, session_id=b_session.session_id,
        config=CONFIG, model=Path("unused"), max_inflated_bytes=0,
    )  # fmt: skip


def debug_dir(ctx: Context) -> Path:
    return ctx.derived / DEBUG_DIR


def assert_recorded_like_a(entry: dict[str, Any]) -> None:
    """A plots stage that did not raise is recorded without an error, with the status the shared
    rule (`status_of`) gives its summary: the same as A's `plots` stage gets."""
    assert entry["error"] is None and entry["status"] == status_of(entry["summary"])
    assert entry["status"] in ("ok", "skipped")  # never "failed": a plot cannot fail the stage


def check_png(path: Path) -> None:
    data = path.read_bytes()
    assert data[:8] == PNG_SIGNATURE and len(data) > 5000, path.name
    image = cv2.imread(str(path))
    assert image is not None, f"{path.name} does not decode"
    assert image.shape[0] > 300 and image.shape[1] > 500, (path.name, image.shape)
    assert image.std() > 10, f"{path.name} looks blank"


# --- the names ---


def test_the_names_of_the_plots_and_the_stage() -> None:
    assert PLOT_FILES == (
        "calibration.png",
        "validation.png",
        "sync.png",
        "signals.png",
        "gaze.png",
    )
    assert PLOT_B_FILES == ("calibration_b.png", "validation_b.png", "gaze_b.png")
    assert set(PLOT_B_CAPTIONS) == set(PLOT_B_FILES) and not set(PLOT_B_FILES) & set(PLOT_CAPTIONS)
    assert all(caption for caption in PLOT_B_CAPTIONS.values())
    assert B_STAGE_NAMES[-1] == "plots_b" and B_STAGE_NAMES[-2] == "quality_b"
    assert set(B_STAGE_DESCRIPTIONS) == set(B_STAGE_NAMES) and tuple(STAGES_B) == B_STAGE_NAMES
    assert tuple(plots.PLOTS_B) == PLOT_B_FILES


# --- the stage on the real session ---


def test_the_stage_draws_the_three_plots_of_b(copy: Context) -> None:
    summary = STAGES_B["plots_b"](copy)
    assert summary == {"written": list(PLOT_B_FILES), "skipped": {}, "errors": {}}
    for name in PLOT_B_FILES:
        check_png(debug_dir(copy) / name)
    assert not list(debug_dir(copy).glob(".*"))  # no temporary files


def test_the_summary_has_the_shape_of_the_plots_summary_of_a(copy: Context) -> None:
    summary_a = run_plots(copy.root, copy.study_id, copy.session_id, CONFIG).as_dict()
    summary_b = STAGES_B["plots_b"](copy)
    assert set(summary_b) == set(summary_a) == {"written", "skipped", "errors"}
    assert summary_a["written"] == list(PLOT_FILES)  # A's own summary is what it was
    assert all(isinstance(summary_b[key], type(summary_a[key])) for key in summary_a)


def test_drawing_b_leaves_the_plots_of_a_as_they_were(copy: Context) -> None:
    before = {name: (debug_dir(copy) / name).read_bytes() for name in PLOT_FILES}
    assert all(before.values())  # A's pipeline drew them
    STAGES_B["plots_b"](copy)
    assert {name: (debug_dir(copy) / name).read_bytes() for name in PLOT_FILES} == before
    # and the other way round: drawing A's plots again does not touch B's
    b_before = {name: (debug_dir(copy) / name).read_bytes() for name in PLOT_B_FILES}
    run_plots(copy.root, copy.study_id, copy.session_id, CONFIG)
    assert {name: (debug_dir(copy) / name).read_bytes() for name in PLOT_B_FILES} == b_before


def test_a_plots_are_still_drawn_with_the_default_spec(copy: Context) -> None:
    default = run_plots(copy.root, copy.study_id, copy.session_id, CONFIG)
    explicit = run_plots(copy.root, copy.study_id, copy.session_id, CONFIG, SPEC_A)
    assert default.as_dict() == explicit.as_dict() and default.written == list(PLOT_FILES)


def test_b_plots_show_b_data(copy: Context) -> None:
    data_b = load_plot_data(copy.root, copy.study_id, copy.session_id, CONFIG, SPEC_B)
    data_a = load_plot_data(copy.root, copy.study_id, copy.session_id, CONFIG)
    assert data_b.spec is SPEC_B and data_a.spec is SPEC_A
    assert data_b.quality is not None and data_b.quality["pipeline"] == "b"  # quality_b.json
    figure = plots.plot_gaze(data_b)
    assert "Pipeline B" in figure.get_suptitle()
    assert "Pipeline" not in plots.plot_gaze(data_a).get_suptitle()
    raw = next(line for line in figure.axes[0].get_lines() if line.get_label() == "raw")
    gaze_b = pq.read_table(copy.derived / SPEC_B.gaze_file)["x_px"].to_pylist()
    expected = np.array([np.nan if v is None else v for v in gaze_b], dtype=float)
    shown = np.asarray(raw.get_ydata(), dtype=float)
    assert np.allclose(shown, expected, equal_nan=True)
    raw_a = next(
        line for line in plots.plot_gaze(data_a).axes[0].get_lines() if line.get_label() == "raw"
    )
    assert not np.allclose(shown, np.asarray(raw_a.get_ydata(), dtype=float), equal_nan=True)
    # the calibration plot is fitted on B's feature vector (six columns), not A's
    calibration = plots.plot_calibration(data_b)
    assert "Pipeline B" in calibration.get_suptitle()
    assert any(ax.axison and "CV" in ax.get_title() for ax in calibration.axes)


# --- the stage in B's runner ---


def test_the_runner_records_the_plots_stage_and_the_quality_report(copy: Context) -> None:
    summaries = run_pipeline_b(copy, only="plots_b")
    assert summaries["plots_b"]["summary"]["written"] == list(PLOT_B_FILES)
    stages = json.loads((copy.derived / STAGES_B_FILE).read_text())["stages"]
    assert_recorded_like_a(stages["plots_b"])
    report = json.loads((copy.derived / QUALITY_B_FILE).read_text())
    assert report["stages"]["plots_b"]["summary"]["written"] == list(PLOT_B_FILES)
    assert report["stages"]["plots_b"]["seconds"] == stages["plots_b"]["seconds"]


def test_a_plot_that_raises_is_recorded_and_the_stage_does_not_fail(
    copy: Context, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(data: Any) -> Figure:
        raise ZeroDivisionError("oops")

    monkeypatch.setitem(plots.PLOTS_B, "gaze_b.png", broken)
    summaries = run_pipeline_b(copy, only="plots_b")
    summary = summaries["plots_b"]["summary"]
    assert summary["errors"] == {"gaze_b.png": "ZeroDivisionError: oops"}
    assert summary["written"] == ["calibration_b.png", "validation_b.png"]
    stages = json.loads((copy.derived / STAGES_B_FILE).read_text())["stages"]
    assert_recorded_like_a(stages["plots_b"])
    report = json.loads((copy.derived / QUALITY_B_FILE).read_text())
    assert report["stages"]["plots_b"]["summary"]["errors"] == summary["errors"]
    assert not (debug_dir(copy) / "gaze_b.png").exists()
    assert any("plot failed" in r.getMessage() for r in caplog.records)


def test_a_failed_calibration_of_b_is_a_recorded_skip_and_old_pictures_go(copy: Context) -> None:
    STAGES_B["plots_b"](copy)
    assert (debug_dir(copy) / "gaze_b.png").is_file()
    # B's calibration fails for real: no dot has the frames this config asks for.
    failing = replace(CONFIG, calib_min_frames_per_dot=100_000, calib_max_sparse_dots=0)
    run_pipeline_b(replace(copy, config=failing), from_stage="calibration_b")
    calibration = json.loads((copy.derived / SPEC_B.calibration_file).read_text())
    assert calibration["status"] == "failed"
    stages = json.loads((copy.derived / STAGES_B_FILE).read_text())["stages"]
    assert stages["gaze_b"]["status"] == "skipped"
    assert_recorded_like_a(stages["plots_b"])
    summary = stages["plots_b"]["summary"]
    assert summary["errors"] == {}
    assert set(summary["skipped"]) == {"validation_b.png", "gaze_b.png"}
    assert "no validation result" in summary["skipped"]["validation_b.png"]
    assert "no gaze" in summary["skipped"]["gaze_b.png"]
    assert summary["written"] == ["calibration_b.png"]  # the attempts, with the reason: no fit
    assert not (debug_dir(copy) / "gaze_b.png").exists()  # a stale picture would mislead
    assert not (debug_dir(copy) / "validation_b.png").exists()
    check_png(debug_dir(copy) / "calibration_b.png")
    assert (debug_dir(copy) / "gaze.png").is_file()  # A's plots stay


def test_without_b_features_every_plot_is_skipped_with_the_reason(copy: Context) -> None:
    (copy.derived / SPEC_B.features_file).unlink()
    summary = STAGES_B["plots_b"](copy)
    assert summary["written"] == [] and set(summary["skipped"]) == set(PLOT_B_FILES)
    assert all("features_b.parquet is missing" in r for r in summary["skipped"].values())


# --- the admin serves B's plots ---


def test_the_debug_route_serves_b_plots_and_only_known_names(copy: Context) -> None:
    STAGES_B["plots_b"](copy)
    session = SimpleNamespace(study_id=copy.study_id)
    db: Any = SimpleNamespace(get=lambda model, key: session if key == copy.session_id else None)
    request: Any = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        settings=SimpleNamespace(data_root=copy.root))))  # fmt: skip
    admin: Any = SimpleNamespace()

    def serve(name: str) -> Any:
        return debug_file(request, copy.session_id, name, admin, db)

    for name in (*PLOT_FILES, *PLOT_B_FILES):
        response = serve(name)
        assert Path(response.path) == debug_dir(copy) / name
        assert response.media_type == "image/png"
    with pytest.raises(NotFoundError, match="no debug file called"):
        serve("../quality_b.json")
    with pytest.raises(NotFoundError, match="no debug file called"):
        serve("secret_b.png")
    (debug_dir(copy) / "gaze_b.png").unlink()
    with pytest.raises(NotFoundError, match="has not been drawn"):
        serve("gaze_b.png")
    with pytest.raises(NotFoundError, match="not found"):
        debug_file(request, uuid.uuid4(), "gaze_b.png", admin, db)
