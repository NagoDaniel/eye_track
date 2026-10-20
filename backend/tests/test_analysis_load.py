"""The per-session gaze the analysis reads, on a session with a known scanpath (PLAN §9)."""

import json
import uuid
from pathlib import Path

import numpy as np
import pytest

from app.storage import derived_dir
from tests.synthetic_session import (
    FIXATIONS,
    VIDEO_RECT,
    Synthetic,
    build,
    drift_at,
)
from worker.analysis.config import AnalysisConfig
from worker.analysis.load import accuracy_px, load_session
from worker.pipeline.config import PipelineConfig
from worker.pipeline.gaze import run_gaze
from worker.pipeline.quality import QUALITY_FILE, build_quality

CONFIG = PipelineConfig()
PLAIN = AnalysisConfig(gaze_columns="plain")
DRIFT = AnalysisConfig(gaze_columns="drift_corrected")


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Synthetic:
    s = build(tmp_path_factory.mktemp("analysis_load"))
    run_gaze(s.root, s.study_id, s.session_id, CONFIG)
    build_quality(s.root, s.study_id, s.session_id, CONFIG, {})
    return s


def test_samples_are_valid_playback_frames_in_video_coordinates(session: Synthetic) -> None:
    g = load_session(session.root, 1, session.session_id, "AB", 1, PLAIN)
    s = g.samples
    assert len(s.t_ms) > 100
    assert np.all(np.diff(s.t_ms) > 0)
    assert s.stim_time.min() >= 0 and s.stim_time.max() < 8.1
    assert np.isfinite(s.x).all() and np.isfinite(s.y).all()
    # The first fixation sits at the screen centre (500, 300): the middle of the video rectangle.
    first = (s.t_ms > 12_100) & (s.t_ms < 12_400)
    assert abs(np.median(s.x[first]) - 0.5) < 0.05 and abs(np.median(s.y[first]) - 0.5) < 0.05
    # The face-lost stretch has no samples
    assert not np.any((s.t_ms > 15_050) & (s.t_ms < 15_350))


def test_each_sample_stands_for_the_time_to_the_next_frame(session: Synthetic) -> None:
    s = load_session(session.root, 1, session.session_id, "AB", 1, PLAIN).samples
    assert len(s.dt_s) == len(s.t_ms)
    assert np.all(s.dt_s > 0) and np.all(s.dt_s <= 0.1 + 1e-12)
    assert np.median(s.dt_s) == pytest.approx(1 / 30, abs=0.002)  # the synthetic webcam: 30 fps
    short = load_session(
        session.root, 1, session.session_id, "AB", 1, AnalysisConfig(sample_max_dt_ms=20)
    )
    assert np.all(short.samples.dt_s <= 0.02 + 1e-12)


def test_another_video_has_no_rows(session: Synthetic) -> None:
    g = load_session(session.root, 1, session.session_id, "AB", 2, PLAIN)
    assert len(g.samples.t_ms) == 0 and len(g.fixations.start_t) == 0
    assert g.video_width_px is None


def test_fixations_match_the_designed_ones(session: Synthetic) -> None:
    f = load_session(session.root, 1, session.session_id, "AB", 1, PLAIN).fixations
    assert len(f.start_t) >= len(FIXATIONS) - 1
    wanted = FIXATIONS[1]  # (12500, 13300, 300, 300) in screen px
    k = int(np.argmin(np.abs(f.start_t - wanted[0])))
    assert abs(f.x[k] - (wanted[2] - VIDEO_RECT["x"]) / VIDEO_RECT["width"]) < 0.05
    assert abs(f.y[k] - wanted[3] / VIDEO_RECT["height"]) < 0.05
    assert f.stim_end[k] > f.stim_start[k] and f.duration_ms[k] >= 100


def test_drift_corrected_columns_remove_the_designed_drift(session: Synthetic) -> None:
    plain = load_session(session.root, 1, session.session_id, "AB", 1, PLAIN).samples
    fixed = load_session(session.root, 1, session.session_id, "AB", 1, DRIFT).samples
    both = np.intersect1d(plain.t_ms, fixed.t_ms)
    assert len(both) > 100
    p = plain.x[np.isin(plain.t_ms, both)]
    c = fixed.x[np.isin(fixed.t_ms, both)]
    expected = drift_at(both) / VIDEO_RECT["width"]
    assert np.median(p - c) == pytest.approx(np.median(expected), abs=0.003)


def test_accuracy_is_in_px_and_in_video_units(session: Synthetic) -> None:
    g = load_session(session.root, 1, session.session_id, "AB", 1, PLAIN)
    quality = json.loads(
        (derived_dir(session.root, 1, session.session_id) / QUALITY_FILE).read_text()
    )
    blocks = quality["validation"]["blocks"]
    assert g.accuracy_px == pytest.approx(np.mean([b["mean_error_px"] for b in blocks]))
    assert g.video_width_px == VIDEO_RECT["width"]
    assert g.accuracy_norm == pytest.approx(g.accuracy_px / VIDEO_RECT["width"])  # type: ignore[operator]
    corrected = load_session(session.root, 1, session.session_id, "AB", 1, DRIFT)
    assert corrected.accuracy_px is not None and corrected.accuracy_px < g.accuracy_px  # type: ignore[operator]


def test_accuracy_without_validation_data_is_none() -> None:
    assert accuracy_px({}, "plain") is None
    assert accuracy_px({"validation": {"blocks": [{"mean_error_px": None}]}}, "plain") is None


def test_a_missing_derived_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_session(tmp_path, 1, uuid.uuid4(), "AB", 1, PLAIN)
