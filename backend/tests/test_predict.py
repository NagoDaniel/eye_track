"""Prediction and validity (PLAN §8.7), and the whole chain on the recorded session."""

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from app.schemas import EventRecord
from worker.pipeline.config import PipelineConfig
from worker.pipeline.predict import predict_gaze, predict_session, viewport_timeline
from worker.pipeline.ridge import fit_ridge
from worker.pipeline.smoothing import smooth_gaze

CONFIG = PipelineConfig()


def viewport_event(t: float, width: float, height: float) -> EventRecord:
    return EventRecord.model_validate(
        {
            "t": t,
            "type": "viewport_change",
            "data": {"viewport": {"width": width, "height": height}},
        }
    )


def identity_model(scale: float = 1.0):  # type: ignore[no-untyped-def]
    """A model that maps features (x, y, ...) to (x * scale, y * scale)."""
    rng = np.random.default_rng(0)
    x = rng.uniform(0, 1000, size=(200, 3))
    y = np.column_stack([x[:, 0] * scale, x[:, 1] * scale])
    return fit_ridge(x, y, 1e-6, 1)


def test_the_viewport_in_force_is_the_last_one_logged() -> None:
    timeline = viewport_timeline([viewport_event(100, 1280, 720), viewport_event(5000, 1920, 1080)])
    width, height = timeline.at(np.array([0.0, 100.0, 4999.0, 5000.0, 9000.0]))
    assert width.tolist() == [1280, 1280, 1280, 1920, 1920]
    assert height.tolist() == [720, 720, 720, 1080, 1080]
    with pytest.raises(ValueError, match="viewport_change"):
        viewport_timeline([])


def test_predictions_are_made_for_usable_frames_only() -> None:
    model = identity_model()
    vectors = np.array([[100.0, 200.0, 0.0], [300.0, 400.0, 0.0], [500.0, 600.0, 0.0]])
    usable = np.array([True, False, True])
    timeline = viewport_timeline([viewport_event(0, 1280, 720)])
    result = predict_gaze(model, vectors, usable, np.array([0.0, 1.0, 2.0]), timeline, CONFIG)
    assert result.x_px[[0, 2]] == pytest.approx([100, 500], abs=0.01)
    assert result.y_px[[0, 2]] == pytest.approx([200, 600], abs=0.01)
    assert np.isnan(result.x_px[1]) and np.isnan(result.y_px[1])
    assert result.valid.tolist() == [True, False, True]  # a blink or a lost face is not valid


def test_a_prediction_is_valid_inside_the_viewport_expanded_by_15_percent() -> None:
    model = identity_model()
    timeline = viewport_timeline([viewport_event(0, 1000, 500)])
    # x in [-150, 1150], y in [-75, 575]
    points = [
        (-150, 0),
        (-160, 0),
        (1150, 100),
        (1160, 100),
        (500, -75),
        (500, -80),
        (500, 575),
        (500, 580),
    ]
    vectors = np.array([[x, y, 0.0] for x, y in points])
    result = predict_gaze(
        model, vectors, np.ones(len(points), bool), np.zeros(len(points)), timeline, CONFIG
    )
    assert result.valid.tolist() == [True, False, True, False, True, False, True, False]
    # The raw prediction is kept for an out-of-range frame.
    assert result.x_px[1] == pytest.approx(-160, abs=0.01)


def test_the_margin_and_the_changing_viewport_are_used() -> None:
    model = identity_model()
    timeline = viewport_timeline([viewport_event(0, 1000, 500), viewport_event(10, 2000, 1000)])
    vectors = np.array([[1100.0, 100.0, 0.0], [1100.0, 100.0, 0.0]])
    t = np.array([5.0, 15.0])
    assert predict_gaze(model, vectors, np.ones(2, bool), t, timeline, CONFIG).valid.tolist() == [
        True,
        True,
    ]
    vectors = np.array([[1200.0, 100.0, 0.0], [1200.0, 100.0, 0.0]])
    result = predict_gaze(model, vectors, np.ones(2, bool), t, timeline, CONFIG)
    assert result.valid.tolist() == [False, True]  # 1200 is outside 1000 + 15 %, inside 2000
    assert result.viewport_width.tolist() == [1000, 2000]
    none = replace(CONFIG, valid_viewport_margin=0.0)
    assert predict_gaze(model, np.array([[1100.0, 100.0, 0.0]]), np.ones(1, bool), np.array([5.0]),
                        timeline, none).valid.tolist() == [False]  # fmt: skip


def test_the_recorded_session_is_predicted_and_smoothed(tmp_path: Path, face_model: Path) -> None:
    """The whole chain on the real recording. The fake camera does not look at the dots, so the
    predictions are not accurate; what is checked is that the stages fit together."""
    from tests.session_fixture import copy_fixture_session
    from worker.pipeline.calibration import run_calibration
    from worker.pipeline.features_a import FEATURES_FILE, compute_features
    from worker.pipeline.ingest import ingest
    from worker.pipeline.landmarks import detect_landmarks
    from worker.pipeline.sync import run_sync

    session = copy_fixture_session(tmp_path)
    ingest(session.root, session.study_id, session.session_id, session.manifest, 32 * 1024 * 1024)
    derived = session.raw.parent / "derived"
    detect_landmarks(face_model, session.raw / "webcam.webm", derived / "landmarks.parquet")
    compute_features(derived / "landmarks.parquet", derived / FEATURES_FILE, CONFIG)
    run_sync(session.root, session.study_id, session.session_id, CONFIG)
    run_calibration(session.root, session.study_id, session.session_id, CONFIG)

    p = predict_session(session.root, session.study_id, session.session_id, CONFIG)

    n = len(p.pts)
    assert n > 200 and p.gaze.valid.sum() > 0.5 * p.usable.sum() > 0
    assert np.isfinite(p.gaze.x_px[p.usable]).all() and np.isnan(p.gaze.x_px[~p.usable]).all()
    assert (p.gaze.valid <= p.usable).all()
    assert np.all(np.diff(p.t_ms) > 0)

    smoothed = smooth_gaze(p.t_ms, p.gaze.x_px, p.gaze.y_px, p.gaze.valid, CONFIG)
    both = p.gaze.valid & ~np.isnan(smoothed.x)
    assert both.sum() > 0.5 * n
    # Smoothing leaves the sample-to-sample jitter smaller than the raw prediction has.
    consecutive = both[1:] & both[:-1]
    raw_jitter = np.abs(np.diff(p.gaze.x_px))[consecutive].mean()
    smooth_jitter = np.abs(np.diff(smoothed.x))[consecutive].mean()
    assert smooth_jitter < raw_jitter
    # An interpolated sample is never valid, and every smoothed sample is valid or interpolated.
    assert not (smoothed.interpolated & p.gaze.valid).any()
    assert ((p.gaze.valid | smoothed.interpolated) == ~np.isnan(smoothed.x)).all()


def test_a_failed_calibration_is_not_predicted(tmp_path: Path) -> None:
    derived = (
        tmp_path / "studies" / "1" / "sessions" / "00000000-0000-0000-0000-000000000001" / "derived"
    )
    derived.mkdir(parents=True)
    (derived / "calibration_a.json").write_text(
        json.dumps({"status": "failed", "failed_reason": "too_few_valid_frames"})
    )
    import uuid

    with pytest.raises(ValueError, match="too_few_valid_frames"):
        predict_session(tmp_path, 1, uuid.UUID(int=1), CONFIG)
