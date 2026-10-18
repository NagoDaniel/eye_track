"""The debug plots (PLAN §12.2): what is drawn, from which data, and what happens without data."""

import json
from pathlib import Path
from typing import Any

import matplotlib.image as mpimg
import numpy as np
import pytest
from matplotlib.figure import Figure

import worker.pipeline.plots as plots
from app.pipeline_names import PLOT_FILES
from tests.session_fixture import FixtureSession, copy_fixture_session
from tests.synthetic_session import Synthetic, build
from worker.pipeline.config import PipelineConfig
from worker.pipeline.gaze import run_gaze
from worker.pipeline.plots import (
    DEBUG_DIR,
    load_plot_data,
    png_bytes,
    rolling_mean,
    rolling_rate,
    run_plots,
    spans_of,
)
from worker.pipeline.quality import build_quality
from worker.pipeline.runner import Context, run_pipeline

CONFIG = PipelineConfig()


# --- helpers ---


def test_spans_of_marks_the_runs() -> None:
    t = np.arange(10) * 100.0
    mask = np.array([0, 1, 1, 0, 0, 1, 0, 0, 1, 1], dtype=bool)
    assert spans_of(mask, t) == [(100.0, 300.0), (500.0, 600.0), (800.0, 1000.0)]
    assert spans_of(np.zeros(5, bool), np.arange(5.0)) == []
    assert spans_of(np.array([], dtype=bool), np.array([])) == []


def test_rolling_mean_ignores_missing_values() -> None:
    t = np.arange(100) * 100.0  # 10 Hz
    v = np.where(np.arange(100) < 50, 1.0, 0.0)
    v[10] = np.nan
    smooth = rolling_mean(v, t, 1.0)
    assert smooth[5] == pytest.approx(1.0) and smooth[80] == pytest.approx(0.0)
    assert 0 < smooth[50] < 1  # the step
    assert np.isfinite(smooth[10])  # the window around the NaN still has values
    assert np.isnan(rolling_mean(np.array([np.nan, np.nan]), np.array([0.0, 100.0]), 1.0)).all()


def test_rolling_rate_is_the_frame_rate() -> None:
    t = np.arange(300) * (1000 / 30)
    assert rolling_rate(t, 2.0)[150] == pytest.approx(30.0, rel=0.05)
    t_gap = np.concatenate([t[:100], t[200:]])  # a 3.3 s hole
    rate = rolling_rate(t_gap, 2.0)
    assert rate[0] > 25 and rate.min() < 20


def test_png_bytes_is_a_png() -> None:
    figure = Figure()
    figure.subplots().plot([0, 1], [0, 1])
    data = png_bytes(figure)
    assert data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > 1000


# --- figures from a session with a known history ---


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory: pytest.TempPathFactory) -> Synthetic:
    s = build(tmp_path_factory.mktemp("plots"))
    run_gaze(s.root, s.study_id, s.session_id, CONFIG)
    build_quality(s.root, s.study_id, s.session_id, CONFIG)
    return s


def data_of(s: Synthetic) -> plots.PlotData:
    return load_plot_data(s.root, s.study_id, s.session_id, CONFIG)


def xdata(line: Any) -> np.ndarray:
    return np.asarray(line.get_xdata(), dtype=float)


def ydata(line: Any) -> np.ndarray:
    return np.asarray(line.get_ydata(), dtype=float)


def titles(figure: Figure) -> list[str]:
    return [ax.get_title() for ax in figure.axes]


def test_the_gaze_plot_shades_what_is_invalid(synthetic: Synthetic) -> None:
    figure = plots.plot_gaze(data_of(synthetic))
    top, bottom = figure.axes
    # Two grey stretches (the lost face, plus the frames before and after the recording's
    # features) and the blink as an interpolated stretch; phase bands underneath.
    colours = [np.asarray(p.get_facecolor(), dtype=float) for p in top.patches]
    assert len(top.patches) > 5
    assert any(abs(c[0] - c[1]) < 0.01 and abs(c[1] - c[2]) < 0.01 and c[3] < 0.5 for c in colours)
    assert {line.get_label() for line in top.get_lines()} >= {"raw", "smoothed"}
    assert bottom.yaxis_inverted() and bottom.get_xlabel() == "session time (s)"
    # The fixation bars are drawn at the bottom of the y panel.
    bars = [line for line in bottom.get_lines() if line.get_linewidth() == 3]
    assert len(bars) == 5


def test_the_signals_plot_has_the_five_panels(synthetic: Synthetic) -> None:
    figure = plots.plot_signals(data_of(synthetic))
    assert len(figure.axes) == 5
    assert [ax.get_ylabel().split("\n")[0] for ax in figure.axes] == [
        "valid gaze", "face size", "head angle (deg)", "brightness", "webcam frames/s",
    ]  # fmt: skip
    rate = ydata(figure.axes[4].get_lines()[0])
    assert np.nanmedian(rate) == pytest.approx(30.0, rel=0.05)
    assert len(figure.axes[2].get_lines()) == 3  # yaw, pitch, roll


def test_the_validation_plot_shows_error_and_drift(synthetic: Synthetic) -> None:
    figure = plots.plot_validation(data_of(synthetic))
    error, drift = figure.axes
    mean = [line for line in error.get_lines() if line.get_label() == "mean error"][0]
    corrected = [line for line in error.get_lines() if "drift correction" in str(line.get_label())][
        0
    ]
    assert len(xdata(mean)) == 2  # the two validation blocks
    assert all(c < m for c, m in zip(ydata(corrected), ydata(mean), strict=True))
    offsets = [line for line in drift.get_lines() if line.get_label() == "x offset"][0]
    assert ydata(offsets)[0] == pytest.approx(10.0, abs=2)
    assert ydata(offsets)[-1] == pytest.approx(30.0, abs=2)


def test_the_sync_plot_shows_the_curve_and_the_windows(synthetic: Synthetic) -> None:
    sync = data_of(synthetic).sync
    sync["curve"] = {"lag_ms": [-20.0, 0.0, 20.0], "r": [0.2, 0.9, None]}
    sync["windows"] = [
        {"pts_center": 30.0, "lag_ms": 5.0, "peak": 0.9, "used": True, "residual_ms": 1.0},
        {"pts_center": 90.0, "lag_ms": 40.0, "peak": 0.1, "used": False, "residual_ms": None},
    ]
    data = data_of(synthetic)
    data.sync = sync
    figure = plots.plot_sync(data)
    curve, windows, residual = figure.axes
    assert list(xdata(curve.get_lines()[0])) == [-20.0, 0.0, 20.0]
    assert np.isnan(ydata(curve.get_lines()[0])[2])  # a lag without data is a gap
    labels = [line.get_label() for line in windows.get_lines()]
    assert "used in the fit" in labels and "left out" in labels
    assert len(residual.containers[0]) == 1  # one bar: the used window


def test_the_calibration_plot_has_a_panel_per_attempt(synthetic: Synthetic) -> None:
    figure = plots.plot_calibration(data_of(synthetic))
    panels = [ax for ax in figure.axes if ax.axison]
    assert len(panels) == 1 and panels[0].get_title().startswith("Attempt 1")


# --- files, skips and errors ---


def test_all_plots_are_written_as_real_images(synthetic: Synthetic) -> None:
    summary = run_plots(synthetic.root, synthetic.study_id, synthetic.session_id, CONFIG)
    assert summary.errors == {} and summary.skipped == {}
    assert sorted(summary.written) == sorted(PLOT_FILES)
    directory = (
        synthetic.root
        / "studies"
        / "1"
        / "sessions"
        / str(synthetic.session_id)
        / "derived"
        / DEBUG_DIR
    )
    for name in PLOT_FILES:
        image = mpimg.imread(directory / name)
        assert image.shape[0] > 300 and image.shape[1] > 500 and image.shape[2] == 4, name
        assert image[..., :3].std() > 0.02, f"{name} looks blank"
    assert not list(directory.glob(".*"))  # no temporary files


def test_plots_without_their_inputs_are_skipped_and_old_pictures_go(tmp_path: Path) -> None:
    s = build(tmp_path)  # no gaze stage, no quality report
    directory = tmp_path / "studies" / "1" / "sessions" / str(s.session_id) / "derived" / DEBUG_DIR
    directory.mkdir(parents=True)
    (directory / "gaze.png").write_bytes(b"stale")
    summary = run_plots(s.root, s.study_id, s.session_id, CONFIG)
    assert set(summary.skipped) == {"validation.png", "gaze.png"}
    assert "no validation result" in summary.skipped["validation.png"]
    assert "no gaze" in summary.skipped["gaze.png"]
    assert sorted(summary.written) == ["calibration.png", "signals.png", "sync.png"]
    assert not (directory / "gaze.png").exists()  # a stale picture would mislead


def test_a_plot_that_raises_is_recorded_and_the_others_are_still_drawn(
    synthetic: Synthetic, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(data: Any) -> Figure:
        raise ZeroDivisionError("oops")

    monkeypatch.setitem(plots.PLOTS, "sync.png", broken)
    summary = run_plots(synthetic.root, synthetic.study_id, synthetic.session_id, CONFIG)
    assert summary.errors == {"sync.png": "ZeroDivisionError: oops"}
    assert sorted(summary.written) == [
        "calibration.png",
        "gaze.png",
        "signals.png",
        "validation.png",
    ]
    assert any("plot failed" in r.getMessage() for r in caplog.records)


def test_a_session_without_features_skips_everything_with_the_reason(tmp_path: Path) -> None:
    s = build(tmp_path)
    derived = tmp_path / "studies" / "1" / "sessions" / str(s.session_id) / "derived"
    (derived / "calibration_a.json").unlink()
    summary = run_plots(s.root, s.study_id, s.session_id, CONFIG)
    assert summary.written == [] and set(summary.skipped) == set(PLOT_FILES)
    assert all("missing" in reason for reason in summary.skipped.values())


# --- the recorded session ---


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory, face_model: Path) -> FixtureSession:
    s = copy_fixture_session(tmp_path_factory.mktemp("recorded"))
    run_pipeline(
        Context(s.root, s.study_id, s.session_id, CONFIG, face_model, 32 * 1024 * 1024, s.manifest)
    )
    return s


def test_the_pipeline_draws_the_plots_of_the_recorded_session(recorded: FixtureSession) -> None:
    directory = recorded.raw.parent / "derived" / DEBUG_DIR
    for name in PLOT_FILES:
        image = mpimg.imread(directory / name)
        assert image.shape[1] > 500 and image[..., :3].std() > 0.02, name
    report = json.loads((recorded.raw.parent / "derived" / "quality.json").read_text())
    assert report["stages"]["plots"]["summary"]["written"] == list(PLOT_FILES)
    assert report["stages"]["plots"]["summary"]["errors"] == {}


def test_the_recorded_session_calibration_plot_shows_the_fit_and_the_validation(
    recorded: FixtureSession,
) -> None:
    data = load_plot_data(recorded.root, recorded.study_id, recorded.session_id, CONFIG)
    figure = plots.plot_calibration(data)
    (panel,) = [ax for ax in figure.axes if ax.axison]
    assert panel.get_title().startswith("Attempt 1 (gate failed)") and "CV" in panel.get_title()
    # 13 calibration dots + 5 validation points, each a target marker; medians and join lines.
    targets = [
        line
        for line in panel.get_lines()
        if line.get_marker() in ("o", "s") and line.get_markerfacecolor() == "none"
    ]
    assert len(targets) == 18
    assert any(line.get_marker() == "^" for line in panel.get_lines())
