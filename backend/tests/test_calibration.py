"""Calibration fit (PLAN §5.5, §8.6, §13: a synthetic known mapping must be recovered)."""

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from app.schemas import EventRecord
from worker.pipeline.calibration import (
    CALIBRATION_FILE,
    Attempt,
    Dot,
    calibration_attempts,
    choose_attempt,
    cross_validate,
    fit_calibration,
    inlier_mask,
    select_model,
    training_samples,
)
from worker.pipeline.config import PipelineConfig
from worker.pipeline.ridge import fit_ridge, predict

CONFIG = PipelineConfig()
WIDTH, HEIGHT = 1280.0, 720.0
POSITIONS = [  # 13 dots: centre first, a 3x3 grid and the four quadrant centres (PLAN §5.5)
    (640, 360), (76.8, 43.2), (640, 43.2), (1203.2, 43.2), (76.8, 360), (1203.2, 360),
    (76.8, 676.8), (640, 676.8), (1203.2, 676.8), (320, 180), (960, 180), (320, 540), (960, 540),
]  # fmt: skip


def ev(t: float, type_: str, **data: object) -> EventRecord:
    return EventRecord.model_validate({"t": t, "type": type_, "data": data})


def gate(t: float, attempt: int, passed: bool | None, block: str = "before_video") -> EventRecord:
    return ev(
        t, "calib_gate_result", block=block, attempt=attempt, passed=passed,
        skipped=passed is None, mean_error_norm=0.05, point_errors=[], valid_frames_per_dot=[],
        reason="ok" if passed else "high_error",
    )  # fmt: skip


def calibration_block(
    t0: float, positions: list[tuple[float, float]] = POSITIONS
) -> list[EventRecord]:
    """Dots shown 1200 ms apart, each clicked 1000 ms after it appeared."""
    out: list[EventRecord] = []
    for i, (x, y) in enumerate(positions):
        t = t0 + i * 1200
        out += [
            ev(t, "calib_dot_show", phase="calibration", id=i, x=x, y=y),
            ev(t + 1000, "calib_dot_click", phase="calibration", id=i),
            ev(t + 1100, "calib_dot_hide", phase="calibration", id=i),
        ]
    return out


def validation_block(t0: float) -> list[EventRecord]:
    return [
        ev(t0 + i * 1200, "calib_dot_show", phase="validation", id=i, x=640.0, y=360.0)
        for i in range(5)
    ]


# --- attempts ---


def three_attempts() -> list[EventRecord]:
    events: list[EventRecord] = [
        ev(0, "viewport_change", viewport={"width": WIDTH, "height": HEIGHT})
    ]
    for k, passed in enumerate([False, True, False]):
        t0 = 1000 + k * 30_000
        events += (
            calibration_block(t0)
            + validation_block(t0 + 17_000)
            + [gate(t0 + 24_000, k + 1, passed)]
        )
    events += validation_block(200_000) + [gate(206_000, 1, True, "after_last_video")]
    return events


def test_attempts_are_the_calibration_blocks_with_their_gate_results() -> None:
    attempts = calibration_attempts(three_attempts())
    assert [a.number for a in attempts] == [1, 2, 3]
    assert [a.passed for a in attempts] == [False, True, False]
    assert all(len(a.dots) == 13 for a in attempts)  # the validation dots are not calibration dots
    assert attempts[0].dots[0] == Dot(0, 640, 360, 2000)  # shown at 1000, clicked 1000 ms later
    assert attempts[1].start_t == 31_000


def test_the_attempt_used_is_the_last_passed_else_the_last() -> None:
    attempts = calibration_attempts(three_attempts())
    used, reason = choose_attempt(attempts) or (None, "")
    assert used is not None and used.number == 2 and reason == "last_passed"
    none_passed = [a for a in attempts if not a.passed]
    used, reason = choose_attempt(none_passed) or (None, "")
    assert used is not None and used.number == 3 and reason == "none_passed_used_last"
    used, reason = choose_attempt(attempts[:1]) or (None, "")
    assert used is not None and used.number == 1 and reason == "only_attempt"
    assert choose_attempt([]) is None


def test_a_study_without_the_gate_has_one_attempt_without_a_verdict() -> None:
    attempts = calibration_attempts(calibration_block(1000) + validation_block(20_000))
    assert len(attempts) == 1 and attempts[0].gate is None and not attempts[0].passed
    assert attempts[0].number == 1


def test_a_restarted_dot_counts_with_its_last_show_and_click() -> None:
    events = [
        ev(1000, "calib_dot_show", phase="calibration", id=0, x=100.0, y=100.0),
        ev(1200, "calib_dot_click", phase="calibration", id=0),
        ev(1300, "calib_dot_hide", phase="calibration", id=0),  # fullscreen left: dot restarts
        ev(5000, "calib_dot_show", phase="calibration", id=0, x=100.0, y=100.0),
        ev(5900, "calib_dot_click", phase="calibration", id=0),
        ev(6000, "calib_dot_hide", phase="calibration", id=0),
        ev(6100, "calib_dot_show", phase="calibration", id=1, x=200.0, y=200.0),  # never clicked
    ]
    (attempt,) = calibration_attempts(events)
    assert attempt.dots == [Dot(0, 100, 100, 5900), Dot(1, 200, 200, None)]


# --- training samples ---


def test_the_training_window_is_700_ms_before_to_100_ms_after_the_click() -> None:
    t = np.array([1299.0, 1300.0, 1500.0, 2000.0, 2100.0, 2101.0])
    vectors = np.arange(len(t), dtype=float)[:, None] * np.ones((1, 3))
    usable = np.array([True, True, True, True, True, True])
    train = training_samples([Dot(0, 10, 20, 2000.0)], t, vectors, usable, CONFIG)
    assert train.features[:, 0].tolist() == [1, 2, 3, 4]  # 1300 to 2100 inclusive
    assert train.targets.tolist() == [[10, 20]] * 4
    assert train.valid_per_dot == [4]


def test_frames_without_a_face_and_blinks_are_not_used_and_dots_do_not_mix() -> None:
    t = np.arange(0.0, 6000.0, 100.0)
    vectors = np.arange(len(t), dtype=float)[:, None] * np.ones((1, 2))
    usable = np.ones(len(t), dtype=bool)
    usable[[16, 17]] = False  # 1600, 1700: no face or a blink
    dots = [Dot(0, 1, 1, 2000.0), Dot(1, 2, 2, 5000.0), Dot(2, 3, 3, None)]
    train = training_samples(dots, t, vectors, usable, CONFIG)
    assert train.valid_per_dot == [7, 9, 0]  # 1300..2100 (9 frames) minus two; 4300..5100
    assert set(train.dot_index.tolist()) == {0, 1}
    assert train.targets[train.dot_index == 1][0].tolist() == [2, 2]


# --- model choice ---


def synthetic(
    rng: np.random.Generator, dots: int, frames: int, truth: str, noise: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Features z (10 columns) per dot with a known mapping to the dot's position."""
    base = rng.normal(size=(dots, 10))
    w = rng.normal(size=(10, 2))
    positions = base @ w
    if truth == "quadratic":
        positions = positions + 0.8 * (base[:, :3] ** 2) @ rng.normal(size=(3, 2))
    positions = 640 + 120 * positions
    index = np.repeat(np.arange(dots), frames)
    features = base[index] + rng.normal(scale=noise, size=(dots * frames, 10))
    return features, positions[index], index.astype(np.int64), positions


def test_a_linear_mapping_is_recovered_and_the_simple_model_is_kept() -> None:
    rng = np.random.default_rng(1)
    x, y, dot, _ = synthetic(rng, dots=30, frames=12, truth="linear", noise=0.02)
    selection = select_model(x, y, dot, CONFIG)
    assert selection.chosen.degree == 1
    assert selection.linear.error_px < 10  # px, on a spread of about 300 px
    assert selection.quadratic.error_px >= selection.linear.error_px - 1e-9
    assert selection.linear.alpha in CONFIG.calib_alphas


def test_a_quadratic_mapping_selects_the_degree_two_model() -> None:
    rng = np.random.default_rng(2)
    x, y, dot, _ = synthetic(rng, dots=60, frames=8, truth="quadratic", noise=0.01)
    selection = select_model(x, y, dot, CONFIG)
    assert selection.chosen.degree == 2
    assert selection.quadratic.error_px < 0.5 * selection.linear.error_px
    assert selection.chosen is selection.quadratic


def test_both_models_errors_are_recorded() -> None:
    rng = np.random.default_rng(3)
    x, y, dot, _ = synthetic(rng, dots=20, frames=6, truth="linear", noise=0.05)
    selection = select_model(x, y, dot, CONFIG)
    assert np.isfinite(selection.linear.error_px) and np.isfinite(selection.quadratic.error_px)
    assert selection.linear.degree == 1 and selection.quadratic.degree == 2


def test_cross_validation_leaves_a_whole_dot_out() -> None:
    # Two dots with different mappings: leaving one out cannot be fitted by the other.
    x = np.array([[0.0], [1.0], [2.0], [3.0]] * 2)
    y = np.array([[0.0, 0.0]] * 4 + [[100.0, 100.0]] * 4)
    dot = np.array([0] * 4 + [1] * 4)
    assert cross_validate(x, y, dot, 1, 1.0) > 40
    assert cross_validate(x, y, np.zeros(8, dtype=np.int64), 1, 1.0) == float("inf")


def test_the_alpha_grid_is_a_config_value() -> None:
    rng = np.random.default_rng(4)
    x, y, dot, _ = synthetic(rng, dots=15, frames=6, truth="linear", noise=0.05)
    only = replace(CONFIG, calib_alphas=(5.0,))
    assert select_model(x, y, dot, only).chosen.alpha == 5.0


def test_outliers_are_found_per_dot() -> None:
    rng = np.random.default_rng(5)
    x, y, dot, _ = synthetic(rng, dots=20, frames=20, truth="linear", noise=0.02)
    wild = np.flatnonzero(dot == 7)[:4]
    x[wild] += 6.0  # four frames of a dot far off (a glance away)
    model = fit_ridge(x, y, 1.0, 1)
    keep = inlier_mask(model, x, dot, CONFIG)
    assert not keep[wild].any()
    assert keep.sum() >= len(x) - 4 - 0.05 * len(x)  # little else is dropped
    # A dot whose frames are all identical has MAD 0: nothing is dropped there.
    flat = np.ones((10, 10))
    assert inlier_mask(fit_ridge(flat + rng.normal(size=(10, 10)), np.zeros((10, 2)), 1.0, 1), flat,
                       np.zeros(10, dtype=np.int64), CONFIG).all()  # fmt: skip


# --- the whole fit on synthetic frames ---


def synthetic_session(
    rng: np.random.Generator,
    events: list[EventRecord],
    attempt: Attempt,
    mapping: np.ndarray,
    noise: float = 0.02,
    outlier_dot: int | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """30 fps frames. Inside a dot's window the features are its position mapped to 10 columns;
    elsewhere they are random (so anything outside the windows would spoil the fit)."""
    t = np.arange(0.0, 400_000.0, 1000 / 30)
    vectors = rng.normal(scale=3.0, size=(len(t), 10))
    for dot in attempt.dots:
        assert dot.click_t is not None
        inside = (t >= dot.click_t - 700) & (t <= dot.click_t + 100)
        u = np.array([dot.x / WIDTH, dot.y / HEIGHT]) - 0.5
        vectors[inside] = u @ mapping + rng.normal(scale=noise, size=(inside.sum(), 10))
        if dot.id == outlier_dot:
            glance = np.flatnonzero(inside)[:5]
            vectors[glance] += 4.0
    return t, vectors, np.ones(len(t), dtype=bool)


def test_a_synthetic_calibration_is_recovered() -> None:
    rng = np.random.default_rng(6)
    events = calibration_block(1000) + validation_block(20_000) + [gate(26_000, 1, True)]
    (attempt,) = calibration_attempts(events)
    mapping = rng.normal(size=(2, 10))
    t, vectors, usable = synthetic_session(rng, events, attempt, mapping)

    fit = fit_calibration(events, t, vectors, usable, CONFIG)

    assert fit.status == "ok" and fit.model is not None and fit.selection is not None
    assert fit.attempt is not None and fit.attempt.number == 1
    assert fit.valid_per_dot == fit.kept_per_dot or sum(fit.kept_per_dot) >= 0.9 * sum(
        fit.valid_per_dot
    )
    assert min(fit.valid_per_dot) >= 20
    # New feature vectors from known positions are mapped back to those positions.
    positions = rng.uniform([100, 60], [1180, 660], size=(50, 2))
    new = (positions / [WIDTH, HEIGHT] - 0.5) @ mapping
    error = np.linalg.norm(predict(fit.model, new) - positions, axis=1)
    assert np.median(error) < 15  # px, on a 1280 x 720 screen
    assert fit.selection.chosen.error_px < 25


def test_samples_far_from_their_dots_median_prediction_are_rejected() -> None:
    rng = np.random.default_rng(7)
    events = calibration_block(1000) + validation_block(20_000) + [gate(26_000, 1, True)]
    (attempt,) = calibration_attempts(events)
    mapping = rng.normal(size=(2, 10))
    t, vectors, usable = synthetic_session(rng, events, attempt, mapping, outlier_dot=4)
    fit = fit_calibration(events, t, vectors, usable, CONFIG)
    assert fit.status == "ok" and fit.model is not None
    # Five glance-away frames in dot 4. The first pass is fitted with them, so it may not see all
    # of them; most go, and few frames of the other dots.
    assert fit.valid_per_dot[4] - fit.kept_per_dot[4] >= 3
    assert fit.as_dict()["samples"]["rejected"] <= 5 + 0.05 * sum(fit.valid_per_dot)
    positions = rng.uniform([100, 60], [1180, 660], size=(50, 2))
    new = (positions / [WIDTH, HEIGHT] - 0.5) @ mapping
    assert np.median(np.linalg.norm(predict(fit.model, new) - positions, axis=1)) < 15


def test_more_than_three_sparse_dots_fail_the_calibration() -> None:
    rng = np.random.default_rng(8)
    events = calibration_block(1000) + validation_block(20_000) + [gate(26_000, 1, True)]
    (attempt,) = calibration_attempts(events)
    t, vectors, usable = synthetic_session(rng, events, attempt, rng.normal(size=(2, 10)))

    def blank(n: int) -> np.ndarray:
        """Face lost in the windows of the first `n` dots except 4 frames each."""
        keep = usable.copy()
        for dot in attempt.dots[:n]:
            assert dot.click_t is not None
            window = np.flatnonzero((t >= dot.click_t - 700) & (t <= dot.click_t + 100))
            keep[window[4:]] = False
        return keep

    assert fit_calibration(events, t, vectors, blank(3), CONFIG).status == "ok"  # 3 are allowed
    failed = fit_calibration(events, t, vectors, blank(4), CONFIG)
    assert failed.status == "failed" and failed.failed_reason == "too_few_valid_frames"
    assert failed.model is None and failed.valid_per_dot[:4] == [4, 4, 4, 4]
    assert json.loads(json.dumps(failed.as_dict()))["model"] is None
    # The limits are config values.
    relaxed = replace(CONFIG, calib_max_sparse_dots=4)
    assert fit_calibration(events, t, vectors, blank(4), relaxed).status == "ok"


def test_a_session_without_calibration_dots_fails_without_crashing() -> None:
    fit = fit_calibration(
        [ev(0, "window_focus")], np.zeros(0), np.zeros((0, 10)), np.zeros(0, bool), CONFIG
    )
    assert fit.status == "failed" and fit.failed_reason == "no_calibration_dots"
    assert fit.as_dict()["attempt_used"] is None


def test_the_last_passed_attempt_is_the_one_fitted() -> None:
    rng = np.random.default_rng(9)
    events = three_attempts()
    attempts = calibration_attempts(events)
    mapping = rng.normal(size=(2, 10))
    t = np.arange(0.0, 300_000.0, 1000 / 30)
    vectors = rng.normal(scale=3.0, size=(len(t), 10))
    for attempt in attempts:  # each attempt maps the position differently; only #2 is a good fit
        scale = 1.0 if attempt.number == 2 else -1.0
        for dot in attempt.dots:
            assert dot.click_t is not None
            inside = (t >= dot.click_t - 700) & (t <= dot.click_t + 100)
            u = np.array([dot.x / WIDTH, dot.y / HEIGHT]) - 0.5
            vectors[inside] = scale * (u @ mapping) + rng.normal(
                scale=0.02, size=(inside.sum(), 10)
            )
    fit = fit_calibration(events, t, vectors, np.ones(len(t), dtype=bool), CONFIG)
    assert fit.attempt is not None and fit.attempt.number == 2
    assert fit.as_dict()["attempt_used"]["reason"] == "last_passed"
    assert fit.viewport == (WIDTH, HEIGHT)
    assert fit.as_dict()["cv"]["error_norm"] == pytest.approx(
        fit.selection.chosen.error_px / np.hypot(WIDTH, HEIGHT)  # type: ignore[union-attr]
    )


# --- the recorded session ---


def test_the_recorded_session_runs_through_the_whole_chain(
    tmp_path: Path, face_model: Path
) -> None:
    """Plumbing on real data: the fake camera does not look at the dots, so the fit is not a good
    one, but every stage up to the calibration runs on the real recording."""
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

    fit = run_calibration(session.root, session.study_id, session.session_id, CONFIG)

    written = json.loads((derived / CALIBRATION_FILE).read_text("utf-8"))
    assert written == json.loads(json.dumps(fit.as_dict()))
    assert written["status"] == "ok", written["failed_reason"]
    assert written["attempt_used"]["attempt"] == 1 and written["attempt_used"]["passed"] is False
    assert written["attempt_used"]["reason"] == "only_attempt"
    assert len(written["dots"]) == 13 and all(d["valid_frames"] >= 5 for d in written["dots"])
    assert written["viewport"] == {"width": 1280.0, "height": 720.0}
    assert written["cv"]["chosen"] in ("linear", "quadratic") and np.isfinite(
        written["cv"]["error_px"]
    )
    assert written["model"]["degree"] in (1, 2) and len(written["model"]["means"]) == 10
    # The server found about as many usable frames per dot as the browser did (8 to 12), since both
    # saw the same webcam frames; the browser's rate was lower, so the server has at least as many.
    browser = [8, 11, 10, 12, 11, 11, 10, 11, 10, 12, 10, 11, 12]
    assert sum(d["valid_frames"] for d in written["dots"]) >= 0.8 * sum(browser)
