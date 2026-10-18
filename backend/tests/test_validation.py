"""Validation accuracy, precision and drift (PLAN §8.8, §13)."""

import json
from pathlib import Path

import numpy as np
import pytest

from app.schemas import EventRecord
from worker.pipeline.config import PipelineConfig
from worker.pipeline.predict import viewport_timeline
from worker.pipeline.validation import (
    DriftModel,
    ValidationDots,
    score_validation,
    validation_blocks,
)

CONFIG = PipelineConfig()
W, H = 1280.0, 720.0
DIAGONAL = float(np.hypot(W, H))
TARGETS = [(640.0, 360.0), (320.0, 180.0), (960.0, 180.0), (320.0, 540.0), (960.0, 540.0)]
VIEWPORT = viewport_timeline(
    [EventRecord.model_validate({"t": 0, "type": "viewport_change",
                                 "data": {"viewport": {"width": W, "height": H}}})]
)  # fmt: skip


def ev(t: float, type_: str, **data: object) -> EventRecord:
    return EventRecord.model_validate({"t": t, "type": type_, "data": data})


def block_events(t0: float) -> list[EventRecord]:
    """A validation block: dots shown 1200 ms apart, each clicked 1000 ms after it appears."""
    out: list[EventRecord] = []
    for i, (x, y) in enumerate(TARGETS):
        t = t0 + i * 1200
        out += [
            ev(t, "calib_dot_show", phase="validation", id=i, x=x, y=y),
            ev(t + 1000, "calib_dot_click", phase="validation", id=i),
            ev(t + 1100, "calib_dot_hide", phase="validation", id=i),
        ]
    return out


SESSION_EVENTS = [
    *block_events(1000),  # before the video
    ev(8000, "video_load", video_id="1"),
    ev(9000, "video_ended", video_id="1"),
    *block_events(10_000),  # between the videos
    ev(20_000, "video_load", video_id="2"),
    ev(21_000, "video_ended", video_id="2"),
    *block_events(22_000),  # after the last video
]


def test_blocks_are_the_validation_runs_and_know_where_they_are() -> None:
    blocks = validation_blocks(SESSION_EVENTS)
    assert [b.kind for b in blocks] == ["before_video", "between_videos", "after_last_video"]
    assert [b.index for b in blocks] == [0, 1, 2]
    assert all(len(b.dots) == 5 for b in blocks)
    assert blocks[0].dots[1].x == 320 and blocks[0].dots[1].click_t == 1000 + 1200 + 1000


def test_calibration_dots_and_earlier_blocks_are_left_out() -> None:
    cal = [ev(500, "calib_dot_show", phase="calibration", id=0, x=1.0, y=1.0),
           ev(600, "calib_dot_click", phase="calibration", id=0)]  # fmt: skip
    gate = ev(
        8000, "calib_gate_result", block="before_video", attempt=1, passed=False, skipped=False,
        mean_error_norm=0.2, point_errors=[], valid_frames_per_dot=[], reason="high_error",
    )  # fmt: skip
    events = [*cal, *block_events(1000), gate, *cal, *block_events(20_000)]
    assert len(validation_blocks(events)) == 2
    later = validation_blocks(events, since_t=15_000)
    assert len(later) == 1 and later[0].start_t == 20_000 and later[0].index == 0


def test_a_study_without_videos_labels_every_block_before_video() -> None:
    assert [b.kind for b in validation_blocks(block_events(0))] == ["before_video"]


def frames(
    blocks: list[ValidationDots], offset: tuple[float, float] | None = None, jitter: float = 0.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """30 fps predictions: in each dot's window the target plus an offset and alternating jitter."""
    t = np.arange(0.0, 30_000.0, 1000 / 30)
    x, y = np.full(len(t), 600.0), np.full(len(t), 300.0)
    for block in blocks:
        for dot in block.dots:
            assert dot.click_t is not None
            inside = np.flatnonzero((t >= dot.click_t - 700) & (t <= dot.click_t + 100))
            sign = np.where(np.arange(len(inside)) % 2 == 0, 1.0, -1.0)
            o = (offset(dot.click_t) if callable(offset) else offset) or (0.0, 0.0)
            x[inside] = dot.x + o[0] + sign * jitter
            y[inside] = dot.y + o[1]
    return t, x, y, np.ones(len(t), bool)


def test_accuracy_and_precision_of_a_point() -> None:
    blocks = validation_blocks(SESSION_EVENTS)[:1]
    t, x, y, valid = frames(blocks, offset=(10.0, 10.0), jitter=3.0)
    result = score_validation(blocks, t, x, y, valid, VIEWPORT, CONFIG)
    point = result.blocks[0].points[0]
    # The median prediction is 10 px right and 10 px down of the target (the jitter alternates
    # around it, so the median is within 3 px of that).
    assert point.error_px == pytest.approx(np.hypot(10, 10), abs=3.0)
    assert point.error_norm == pytest.approx(point.error_px / DIAGONAL if point.error_px else 0)
    # x alternates by +-3: consecutive samples are 6 px apart.
    assert point.precision_px == pytest.approx(6.0)
    assert point.frames == 25  # 800 ms at 30 fps (both ends of the window)
    assert result.blocks[0].as_dict()["mean_error_deg"] is None


def test_exact_values_for_a_known_median_and_steps() -> None:
    blocks = validation_blocks(SESSION_EVENTS)[:1]
    t = np.arange(0.0, 8000.0, 100.0)  # 10 Hz so the window holds 9 frames
    x, y = np.full(len(t), 0.0), np.full(len(t), 0.0)
    dot = blocks[0].dots[0]
    assert dot.click_t is not None
    idx = np.flatnonzero((t >= dot.click_t - 700) & (t <= dot.click_t + 100))
    assert len(idx) == 9
    x[idx] = dot.x + np.array([0, 3, 0, 3, 0, 3, 0, 3, 0])  # median = target + 0
    y[idx] = dot.y + 4.0  # a constant 4 px below: median offset (0, 4)
    point = (
        score_validation(blocks, t, x, y, np.ones(len(t), bool), VIEWPORT, CONFIG)
        .blocks[0]
        .points[0]
    )
    assert (point.median_x, point.median_y) == (dot.x, dot.y + 4.0)
    assert point.error_px == pytest.approx(4.0)
    assert point.precision_px == pytest.approx(3.0)  # every step is 3 px in x, 0 in y


def test_only_the_window_and_valid_frames_count() -> None:
    blocks = validation_blocks(SESSION_EVENTS)[:1]
    t, x, y, valid = frames(blocks, offset=(0.0, 0.0))
    dot = blocks[0].dots[0]
    assert dot.click_t is not None
    inside = (t >= dot.click_t - 700) & (t <= dot.click_t + 100)
    x[(t > dot.click_t + 100) & (t < dot.click_t + 400)] = 5000.0  # after the window
    valid[np.flatnonzero(inside)[:5]] = False
    x[np.flatnonzero(inside)[:5]] = 9999.0  # invalid frames inside the window
    point = score_validation(blocks, t, x, y, valid, VIEWPORT, CONFIG).blocks[0].points[0]
    assert point.error_px == pytest.approx(0.0) and point.frames == 20


def test_precision_uses_frames_that_follow_each_other() -> None:
    blocks = validation_blocks(SESSION_EVENTS)[:1]
    t, x, y, valid = frames(blocks, offset=(0.0, 0.0))
    dot = blocks[0].dots[0]
    assert dot.click_t is not None
    inside = np.flatnonzero((t >= dot.click_t - 700) & (t <= dot.click_t + 100))
    x[inside[::2]] += 20.0  # every other frame is 20 px off ...
    valid[inside[1::2]] = False  # ... and the frames between are invalid: no consecutive pair
    point = score_validation(blocks, t, x, y, valid, VIEWPORT, CONFIG).blocks[0].points[0]
    assert point.precision_px is None and point.error_px is not None


def test_points_without_data_have_no_error_and_do_not_count_in_the_mean() -> None:
    blocks = validation_blocks(SESSION_EVENTS)[:1]
    t, x, y, valid = frames(blocks, offset=(6.0, 8.0))
    dot = blocks[0].dots[2]
    assert dot.click_t is not None
    valid[(t >= dot.click_t - 700) & (t <= dot.click_t + 100)] = False
    result = score_validation(blocks, t, x, y, valid, VIEWPORT, CONFIG)
    assert result.blocks[0].points[2].error_px is None
    d = result.blocks[0].as_dict()
    assert d["points_with_data"] == 4 and d["mean_error_px"] == pytest.approx(10.0)
    none = score_validation(blocks, t, x, y, np.zeros(len(t), bool), VIEWPORT, CONFIG)
    assert none.drift is None and none.precision_px is None
    assert none.blocks[0].as_dict()["mean_error_px"] is None


def test_the_error_is_normalised_by_the_viewport_at_the_time() -> None:
    blocks = validation_blocks(SESSION_EVENTS)[:1]
    t, x, y, valid = frames(blocks, offset=(30.0, 40.0))  # 50 px
    big = viewport_timeline(
        [ev(0, "viewport_change", viewport={"width": 1920.0, "height": 1080.0})]
    )
    result = score_validation(blocks, t, x, y, valid, big, CONFIG)
    assert result.blocks[0].points[0].error_norm == pytest.approx(50 / np.hypot(1920, 1080))


# --- drift ----------------------------------------------------------------------------------------


def test_drift_is_the_mean_offset_of_each_block_and_is_removed_by_the_correction() -> None:
    blocks = validation_blocks(SESSION_EVENTS)
    # The gaze drifts steadily: +3 px in x and -2 px in y per second.
    drifting = lambda t: (3.0 * t / 1000, -2.0 * t / 1000)  # noqa: E731
    t, x, y, valid = frames(blocks, offset=drifting)  # type: ignore[arg-type]
    result = score_validation(blocks, t, x, y, valid, VIEWPORT, CONFIG)
    assert result.drift is not None
    offsets = [b.offset for b in result.blocks]
    assert all(o is not None for o in offsets)
    # Block 3 (about 22.9 s) is further off than block 1 (about 3.4 s).
    assert offsets[2][0] > offsets[1][0] > offsets[0][0] > 0  # type: ignore[index]
    uncorrected = [b.as_dict()["mean_error_px"] for b in result.blocks]
    corrected = [b.as_dict()["mean_error_corrected_px"] for b in result.blocks]
    assert all(u > 10 for u in uncorrected)
    assert all(c < 0.2 * u for c, u in zip(corrected, uncorrected, strict=True))
    assert result.as_dict()["blocks"][0]["mean_error_corrected_norm"] is not None


def test_the_offset_is_interpolated_between_blocks_and_constant_outside() -> None:
    drift = DriftModel(np.array([1000.0, 3000.0]), np.array([10.0, 30.0]), np.array([0.0, -20.0]))
    dx, dy = drift.offset_at(np.array([0.0, 1000.0, 2000.0, 3000.0, 9000.0]))
    assert dx.tolist() == [10, 10, 20, 30, 30]
    assert dy.tolist() == [0, 0, -10, -20, -20]
    one = DriftModel(np.array([1000.0]), np.array([7.0]), np.array([1.0]))
    assert one.offset_at(np.array([0.0, 5000.0]))[0].tolist() == [7, 7]


def test_the_correction_subtracts_the_offset_from_the_gaze() -> None:
    drift = DriftModel(np.array([0.0, 1000.0]), np.array([0.0, 10.0]), np.array([0.0, 20.0]))
    x, y = drift.correct(np.array([500.0]), np.array([100.0]), np.array([100.0]))
    assert (x[0], y[0]) == (95.0, 90.0)


def test_the_session_precision_is_the_median_over_the_blocks() -> None:
    blocks = validation_blocks(SESSION_EVENTS)
    t, x, y, valid = frames(blocks)
    # Jitter of 1, 2 and 10 px in the three blocks: steps of 2, 4 and 20 px.
    for block, jitter in zip(blocks, (1.0, 2.0, 10.0), strict=True):
        for dot in block.dots:
            assert dot.click_t is not None
            inside = np.flatnonzero((t >= dot.click_t - 700) & (t <= dot.click_t + 100))
            x[inside] = dot.x + np.where(np.arange(len(inside)) % 2 == 0, jitter, -jitter)
    result = score_validation(blocks, t, x, y, valid, VIEWPORT, CONFIG)
    assert result.precision_px == pytest.approx(4.0)
    assert json.loads(json.dumps(result.as_dict()))["precision_px"] == pytest.approx(4.0)


# --- the recorded session ---------------------------------------------------------------


def test_the_recorded_session_is_scored(tmp_path: Path, face_model: Path) -> None:
    from tests.session_fixture import copy_fixture_session
    from worker.pipeline.calibration import run_calibration
    from worker.pipeline.features_a import FEATURES_FILE, compute_features
    from worker.pipeline.ingest import ingest
    from worker.pipeline.landmarks import detect_landmarks
    from worker.pipeline.sync import run_sync
    from worker.pipeline.validation import validate_session

    session = copy_fixture_session(tmp_path)
    ingest(session.root, session.study_id, session.session_id, session.manifest, 32 * 1024 * 1024)
    derived = session.raw.parent / "derived"
    detect_landmarks(face_model, session.raw / "webcam.webm", derived / "landmarks.parquet")
    compute_features(derived / "landmarks.parquet", derived / FEATURES_FILE, CONFIG)
    run_sync(session.root, session.study_id, session.session_id, CONFIG)
    run_calibration(session.root, session.study_id, session.session_id, CONFIG)

    result = validate_session(session.root, session.study_id, session.session_id, CONFIG)

    # The session has the pre-video block and the one after the video (it ran no block in between).
    assert [b.kind for b in result.blocks] == ["before_video", "after_last_video"]
    assert all(len(b.points) == 5 for b in result.blocks)
    # The fake camera is not looking at the dots, so the model is a poor one: in the first block it
    # still predicts on the screen, in the last one (the head has turned) it extrapolates far off
    # screen and the validity rule leaves those frames out (all five points can end up empty).
    d = result.as_dict()
    first, last = d["blocks"]
    assert first["points_with_data"] >= 3 and first["mean_error_norm"] > 0
    assert last["points_with_data"] <= 5
    with_data = [b for b in d["blocks"] if b["points_with_data"]]
    assert d["drift"] is not None and len(d["drift"]["t_ms"]) == len(with_data)
    assert result.precision_px is not None and result.precision_px >= 0
    json.dumps(d)
