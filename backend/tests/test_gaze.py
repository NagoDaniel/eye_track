"""The assembled gaze table and the fixations (PLAN §7, §8.9) on a session with a known scanpath."""

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.schemas import EventRecord
from tests.synthetic_session import (
    BLINK,
    DRIFT_FIRST,
    FACE_LOST,
    FIXATIONS,
    VIDEO_END_MS,
    VIDEO_FIRST_FRAME_MS,
    VIDEO_RECT,
    VIDEO_START_MS,
    Synthetic,
    build,
    drift_at,
)
from worker.pipeline.config import PipelineConfig
from worker.pipeline.gaze import (
    FIXATIONS_FILE,
    GAZE_FILE,
    confidence,
    frame_phases,
    phase_intervals,
    run_gaze,
)

CONFIG = PipelineConfig()


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Synthetic:
    return build(tmp_path_factory.mktemp("synthetic"))


@pytest.fixture(scope="module")
def gaze(session: Synthetic) -> pa.Table:
    run_gaze(session.root, session.study_id, session.session_id, CONFIG)
    derived = session.root / "studies" / "1" / "sessions" / str(session.session_id) / "derived"
    return pq.read_table(derived / GAZE_FILE)


@pytest.fixture(scope="module")
def fixations(session: Synthetic, gaze: pa.Table) -> pa.Table:
    derived = session.root / "studies" / "1" / "sessions" / str(session.session_id) / "derived"
    return pq.read_table(derived / FIXATIONS_FILE)


def col(table: pa.Table, name: str) -> np.ndarray:
    return np.asarray(table[name].to_numpy(zero_copy_only=False))


def test_the_table_has_the_plans_columns_and_one_row_per_frame(
    gaze: pa.Table, session: Synthetic
) -> None:
    schema = gaze.schema
    plan = {
        "t": pa.float64(), "webcam_pts": pa.float64(), "video_id": pa.string(),
        "stim_time": pa.float64(), "x_px": pa.float32(), "y_px": pa.float32(),
        "x_sm": pa.float32(), "y_sm": pa.float32(), "x_vid": pa.float32(), "y_vid": pa.float32(),
        "valid": pa.bool_(), "blink": pa.bool_(), "conf": pa.float32(), "phase": pa.string(),
    }  # fmt: skip
    for name, type_ in plan.items():
        assert schema.field(name).type == type_, name
    assert gaze.column_names[:14] == list(plan)  # the plan's columns come first, in its order
    assert {"interpolated", "x_sm_dc", "y_sm_dc", "x_vid_dc", "y_vid_dc"} <= set(gaze.column_names)
    assert gaze.num_rows == len(session.t)
    np.testing.assert_allclose(col(gaze, "t"), session.t)
    meta = json.loads(schema.metadata[b"gaze"])
    assert meta["drift_corrected"] is True and meta["config"]["fix_min_duration_ms"] == 100.0


def test_phases_follow_the_dot_blocks_and_the_video(gaze: pa.Table) -> None:
    t, phase = col(gaze, "t"), col(gaze, "phase")

    def at(ms: float) -> str:
        return str(phase[int(np.argmin(np.abs(t - ms)))])

    assert at(500) == "other" and at(2000) == "calibration" and at(6000) == "validation"
    assert at(11_500) == "other"  # between the validation block and the video
    assert at(VIDEO_FIRST_FRAME_MS - 50) == "other"  # the video counts from its first frame
    assert at(14_000) == "playback" and at(VIDEO_END_MS + 300) == "other"
    assert at(22_000) == "validation" and at(29_000) == "other"


def test_video_time_and_coordinates_in_the_video_rectangle(gaze: pa.Table) -> None:
    t, vid, stim = col(gaze, "t"), col(gaze, "video_id"), col(gaze, "stim_time")
    playing = (t >= VIDEO_FIRST_FRAME_MS) & (t < VIDEO_END_MS - 100)
    assert all(v == "1" for v in vid[playing])
    before, after = t < VIDEO_START_MS, t > VIDEO_END_MS + 100
    assert all(v is None for v in vid[before]) and all(v is None for v in vid[after])
    np.testing.assert_allclose(stim[playing], (t[playing] - VIDEO_FIRST_FRAME_MS) / 1000, atol=1e-6)
    assert np.isnan(stim[before]).all()
    x_sm, y_sm = col(gaze, "x_sm"), col(gaze, "y_sm")
    x_vid, y_vid = col(gaze, "x_vid"), col(gaze, "y_vid")
    ok = playing & ~np.isnan(x_sm)
    np.testing.assert_allclose(
        x_vid[ok], (x_sm[ok] - VIDEO_RECT["x"]) / VIDEO_RECT["width"], atol=1e-4
    )
    np.testing.assert_allclose(y_vid[ok], y_sm[ok] / VIDEO_RECT["height"], atol=1e-4)
    assert np.isnan(x_vid[before]).all() and np.isnan(x_vid[after]).all()


def test_validity_blinks_interpolation_and_confidence(gaze: pa.Table) -> None:
    t = col(gaze, "t")
    valid, blink, conf = col(gaze, "valid"), col(gaze, "blink"), col(gaze, "conf")
    interpolated = col(gaze, "interpolated")
    in_blink = (t >= BLINK[0]) & (t < BLINK[1])
    lost = (t >= FACE_LOST[0]) & (t < FACE_LOST[1])
    assert blink[in_blink].all() and not blink[~in_blink].any()
    assert not valid[in_blink].any() and not valid[lost].any()
    # A short gap (3 frames) is filled; the long one is not.
    assert interpolated[in_blink].all() and not interpolated[lost].any()
    assert (
        np.isnan(col(gaze, "x_sm")[lost]).all() and np.isfinite(col(gaze, "x_sm")[in_blink]).all()
    )
    assert (conf[in_blink] == 0.5).all() and (conf[lost] == 0).all()
    assert (conf[valid] > 0.99).all()  # all predictions are inside the viewport
    assert np.isnan(col(gaze, "x_px")[lost]).all()


def test_drift_corrected_columns_remove_the_growing_offset(
    gaze: pa.Table, session: Synthetic
) -> None:
    t = col(gaze, "t")
    ok = (t > 12_200) & (t < 15_000) & ~np.isnan(col(gaze, "x_sm"))
    raw, corrected = col(gaze, "x_sm")[ok], col(gaze, "x_sm_dc")[ok]
    # The offset grows from 10 px at the first block to 30 px at the second; what the correction
    # removes is that line, as measured at the two blocks (within their noise).
    removed = raw - corrected
    assert np.abs(removed - drift_at(t[ok])).max() < 2.0
    assert removed.min() > 14 and removed.max() < 19
    truth = session.truth_x[: len(t)][ok]
    smoothing_lag = np.abs(corrected - truth)
    assert np.median(smoothing_lag) < 4  # px
    assert np.median(np.abs(raw - truth)) > 10  # the uncorrected column keeps the drift


def test_fixations_are_found_with_corrected_centres_and_video_times(fixations: pa.Table) -> None:
    rows = fixations.to_pylist()
    assert len(rows) == len(FIXATIONS)  # the blink does not split the second one
    for row, (start, end, fx, fy) in zip(rows, FIXATIONS, strict=True):
        assert row["video_id"] == "1"
        assert row["start_t"] == pytest.approx(max(start, VIDEO_FIRST_FRAME_MS), abs=3 * 33.4)
        assert row["end_t"] == pytest.approx(end, abs=3 * 33.4)
        assert row["duration_ms"] == pytest.approx(row["end_t"] - row["start_t"])
        assert row["cx_px_dc"] == pytest.approx(fx, abs=5) and row["cy_px_dc"] == pytest.approx(
            fy, abs=5
        )
        assert (
            row["cx_px"] - row["cx_px_dc"] > DRIFT_FIRST
        )  # the uncorrected centre keeps the drift
        assert row["cx_vid_dc"] == pytest.approx((fx - 100) / 800, abs=0.01)
        assert row["cy_vid_dc"] == pytest.approx(fy / 600, abs=0.01)
        assert row["stim_start"] == pytest.approx(
            (row["start_t"] - VIDEO_FIRST_FRAME_MS) / 1000, abs=0.05
        )
        assert row["stim_end"] == pytest.approx(
            (row["end_t"] - VIDEO_FIRST_FRAME_MS) / 1000, abs=0.05
        )
        assert row["dispersion_px"] <= 50.0
    assert fixations.column_names[:3] == ["id", "start_t", "end_t"]


def test_the_fixation_file_records_its_thresholds(fixations: pa.Table) -> None:
    meta = json.loads(fixations.schema.metadata[b"fixations"])
    assert meta["algorithm"] == "I-DT" and meta["min_duration_ms"] == 100.0
    assert meta["dispersion_width_fraction"] == 0.05 and meta["precision_factor"] == 2.0
    assert meta["validation_precision_px"] is not None


def test_without_validation_blocks_the_corrected_columns_are_null(tmp_path: Path) -> None:
    s = build(tmp_path, with_validation=False)
    summary = run_gaze(s.root, s.study_id, s.session_id, CONFIG)
    assert summary.drift_corrected is False and summary.validation_precision_px is None
    derived = tmp_path / "studies" / "1" / "sessions" / str(s.session_id) / "derived"
    table = pq.read_table(derived / GAZE_FILE)
    assert np.isnan(col(table, "x_sm_dc")).all() and np.isfinite(col(table, "x_sm")).any()
    rows = pq.read_table(derived / FIXATIONS_FILE).to_pylist()
    assert len(rows) == len(FIXATIONS) and all(r["cx_px_dc"] is None for r in rows)
    assert all(r["cx_px"] is not None for r in rows)


def test_a_failed_calibration_is_not_assembled(tmp_path: Path) -> None:
    s = build(tmp_path)
    derived = tmp_path / "studies" / "1" / "sessions" / str(s.session_id) / "derived"
    (derived / "calibration_a.json").write_text(
        json.dumps({"status": "failed", "failed_reason": "no_calibration_dots"})
    )
    with pytest.raises(ValueError, match="no_calibration_dots"):
        run_gaze(s.root, s.study_id, s.session_id, CONFIG)


def test_confidence_rules() -> None:
    x = np.array([500.0, 500.0, -75.0, -150.0, 500.0, 500.0])  # width 1000, margin 15 %
    y = np.array([300.0, 300.0, 300.0, 300.0, 300.0, 300.0])
    valid = np.array([True, False, True, True, False, True])
    interpolated = np.array([False, True, False, False, False, False])
    width, height = np.full(6, 1000.0), np.full(6, 600.0)
    conf = confidence(x, y, valid, interpolated, width, height, CONFIG)
    assert conf.tolist() == pytest.approx([1.0, 0.5, 0.5, 0.0, 0.0, 1.0])


def test_phase_helpers() -> None:
    from tests.synthetic_session import dot_events

    events = [
        EventRecord.model_validate(e)
        for e in dot_events(100, "calibration", [(1.0, 1.0)] * 2)
        + dot_events(5000, "validation", [(1.0, 1.0)])
    ]
    spans = phase_intervals(events)
    assert spans == [(100.0, 2700.0, "calibration"), (5000.0, 6100.0, "validation")]
    t = np.array([50.0, 300.0, 3000.0, 5500.0, 7000.0])
    on = np.array([False, False, True, False, True])
    assert frame_phases(t, spans, on).tolist() == [
        "other",
        "calibration",
        "playback",
        "validation",
        "playback",
    ]


def test_the_recorded_session_is_assembled(tmp_path: Path, face_model: Path) -> None:
    """The whole pipeline up to the gaze table on the real recording (the fake camera does not look
    at the dots, so what is checked is that the real files fit together)."""
    from tests.session_fixture import copy_fixture_session
    from worker.pipeline.calibration import run_calibration
    from worker.pipeline.features_a import FEATURES_FILE, compute_features
    from worker.pipeline.ingest import ingest
    from worker.pipeline.landmarks import detect_landmarks
    from worker.pipeline.sync import run_sync

    s = copy_fixture_session(tmp_path)
    ingest(s.root, s.study_id, s.session_id, s.manifest, 32 * 1024 * 1024)
    derived = s.raw.parent / "derived"
    detect_landmarks(face_model, s.raw / "webcam.webm", derived / "landmarks.parquet")
    compute_features(derived / "landmarks.parquet", derived / FEATURES_FILE, CONFIG)
    run_sync(s.root, s.study_id, s.session_id, CONFIG)
    run_calibration(s.root, s.study_id, s.session_id, CONFIG)

    summary = run_gaze(s.root, s.study_id, s.session_id, CONFIG)

    table = pq.read_table(derived / GAZE_FILE)
    assert summary.frames == table.num_rows > 200
    phases = set(col(table, "phase"))
    assert phases <= {"calibration", "validation", "playback", "other"}
    assert {"calibration", "validation", "playback"} <= phases
    video = col(table, "video_id")
    assert {v for v in video if v is not None} == {"1"}
    stim = col(table, "stim_time")[[v == "1" for v in video]]
    stim = stim[~np.isnan(stim)]
    assert len(stim) > 50 and stim.min() >= 0 and stim.max() <= 3.0  # the 3 s test clip
    assert (np.diff(stim) >= 0).all()
    assert summary.playback_frames > 50
    assert pq.read_table(derived / FIXATIONS_FILE).num_rows == summary.fixations
