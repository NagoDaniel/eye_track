"""Sync: webcam frame time to client time (PLAN §8.2 steps 1 to 4, §13).

Synthetic signals with an injected offset and drift must be recovered; on the recorded session the
browser's and the server's signals of the same face must line up.
"""

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from worker.pipeline.config import PipelineConfig
from worker.pipeline.sync import SyncResult, estimate_sync

CONFIG = PipelineConfig()
# 30 s windows keep the tests with several windows short; the drift test uses the default 60 s.
SHORT_WINDOWS = replace(CONFIG, sync_window_s=30.0, sync_min_window_s=15.0)
T_REC = 5000.0  # recorder_start on the client clock, ms
OFFSET_MS = 230.0


def smooth_signal(rng: np.random.Generator) -> Callable[[np.ndarray], np.ndarray]:
    """Two channels of band-limited noise (up to 2.5 Hz): a sum of random sinusoids."""
    freqs = rng.uniform(0.1, 2.5, size=(2, 40))
    phases = rng.uniform(0, 2 * np.pi, size=(2, 40))
    amps = rng.uniform(0.5, 1.5, size=(2, 40))

    def at(pts: np.ndarray) -> np.ndarray:
        out = np.stack(
            [
                (amps[c] * np.sin(2 * np.pi * np.outer(pts, freqs[c]) + phases[c])).sum(axis=1)
                for c in range(2)
            ],
            axis=1,
        )
        return out

    return at


def make_session(
    seconds: float,
    offset_ms: float = OFFSET_MS,
    drift_ms_per_s: float = 0.0,
    noise: float = 0.05,
    seed: int = 1,
    missing: float = 0.05,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Server frames (pts, signal) and browser frames (t, signal) of one face.

    The true map is t = T_REC + 1000 * pts + offset + drift * pts. The browser samples at its own
    irregular times; both lose stretches where there was no face.
    """
    rng = np.random.default_rng(seed)
    signal = smooth_signal(rng)
    pts = np.cumsum(rng.uniform(1 / 31, 1 / 29, size=int(seconds * 30)))
    server = signal(pts) + rng.normal(0, noise, size=(len(pts), 2))
    n_browser = int((seconds * 1000 + 1000) / 41.0)
    t_browser = T_REC - 500 + np.arange(n_browser) * 41.0 + rng.uniform(-6, 6, size=n_browser)
    pts_of_browser = (t_browser - T_REC - offset_ms) / (1000 + drift_ms_per_s)
    inside = (pts_of_browser >= 0) & (pts_of_browser <= pts[-1])
    t_browser, pts_of_browser = t_browser[inside], pts_of_browser[inside]
    browser = signal(pts_of_browser) + rng.normal(0, noise, size=(len(t_browser), 2))
    for values in (server, browser):
        for _ in range(int(missing * len(values) / 15)):  # runs of about 0.5 s without a face
            start = rng.integers(0, len(values) - 15)
            values[start : start + 15] = np.nan
    return pts, server, t_browser, browser


def implied_offset_ms(result: SyncResult, pts: float) -> float:
    """The offset from the coarse map (recorder start + pts) that the result applies at `pts`."""
    return float(result.pts_to_t_ms(np.array([pts]))[0] - (T_REC + 1000 * pts))


def test_an_injected_offset_is_recovered() -> None:
    pts, server, t_b, browser = make_session(60, offset_ms=230)
    result = estimate_sync(pts, server, t_b, browser, T_REC, CONFIG)
    assert implied_offset_ms(result, 30) == pytest.approx(230, abs=6)
    assert result.peak is not None and result.peak > 0.9
    assert "sync_short" in result.flags  # 60 s are less than two windows: one offset, slope 1
    assert result.b == 1.0
    assert len(result.windows) == 1


@pytest.mark.parametrize("offset", [-640.0, 0.0, 880.0])
def test_offsets_across_the_search_range(offset: float) -> None:
    pts, server, t_b, browser = make_session(40, offset_ms=offset, seed=3)
    result = estimate_sync(pts, server, t_b, browser, T_REC, CONFIG)
    assert implied_offset_ms(result, 20) == pytest.approx(offset, abs=6)


def test_an_injected_drift_is_recovered() -> None:
    # 0.4 ms per second: the webcam clock runs 0.04 % fast, 120 ms in five minutes.
    pts, server, t_b, browser = make_session(300, offset_ms=230, drift_ms_per_s=0.4, seed=2)
    result = estimate_sync(pts, server, t_b, browser, T_REC, CONFIG)
    assert result.b == pytest.approx(1.0004, abs=8e-5)
    for p in (5, 150, 290):
        assert implied_offset_ms(result, p) == pytest.approx(230 + 0.4 * p, abs=8)
    assert result.flags == []
    assert len(result.windows) == 5 and all(w.used for w in result.windows)
    assert result.residual_max_ms < 20


def test_too_much_drift_is_flagged() -> None:
    pts, server, t_b, browser = make_session(180, drift_ms_per_s=1.6, seed=4)  # b = 1.0016
    result = estimate_sync(pts, server, t_b, browser, T_REC, SHORT_WINDOWS)
    assert result.b == pytest.approx(1.0016, abs=1e-4)
    assert "sync_drift_high" in result.flags


def test_a_window_that_does_not_fit_the_line_is_flagged_and_does_not_bend_it() -> None:
    pts, server, t_b, browser = make_session(180, drift_ms_per_s=0.2, seed=5)
    # A 30 s stretch of the browser's signal is shifted by 200 ms (a glitch in the browser clock).
    glitch = (t_b > T_REC + 60_000) & (t_b < T_REC + 90_000)
    t_b = np.where(glitch, t_b + 200, t_b)
    order = np.argsort(t_b)
    result = estimate_sync(pts, server, t_b[order], browser[order], T_REC, SHORT_WINDOWS)
    assert "sync_residual_high" in result.flags
    assert result.residual_max_ms > CONFIG.sync_max_residual_ms
    assert result.b == pytest.approx(1.0002, abs=3e-4)  # the robust fit follows the other windows


def test_signals_that_do_not_belong_together_are_flagged_not_trusted() -> None:
    pts, server, _, _ = make_session(60, seed=1)
    _, _, t_b, browser = make_session(60, seed=99)  # another face
    result = estimate_sync(pts, server, t_b, browser, T_REC, CONFIG)
    assert "sync_weak_peak" in result.flags
    assert result.peak is not None and result.peak < 0.5


def test_an_offset_just_outside_the_search_is_flagged_at_the_limit() -> None:
    pts, server, t_b, browser = make_session(40, offset_ms=1100, seed=6)
    result = estimate_sync(pts, server, t_b, browser, T_REC, CONFIG)
    assert "sync_lag_at_limit" in result.flags


def test_an_offset_far_outside_the_search_is_not_taken_for_a_match() -> None:
    pts, server, t_b, browser = make_session(40, offset_ms=2500, seed=6)
    result = estimate_sync(pts, server, t_b, browser, T_REC, CONFIG)
    assert {"sync_weak_peak", "sync_lag_at_limit"} & set(result.flags)


def test_without_browser_features_only_the_coarse_offset_is_known() -> None:
    pts, server, _, _ = make_session(30)
    result = estimate_sync(pts, server, np.array([]), np.empty((0, 2)), T_REC, CONFIG)
    assert result.flags == ["sync_coarse_only"]
    assert result.peak is None and result.b == 1.0
    assert implied_offset_ms(result, 10) == 0.0


def test_the_search_range_is_a_config_value() -> None:
    pts, server, t_b, browser = make_session(40, offset_ms=600, seed=7)
    narrow = replace(CONFIG, sync_max_lag_ms=500.0)
    result = estimate_sync(pts, server, t_b, browser, T_REC, narrow)
    assert "sync_lag_at_limit" in result.flags  # 600 ms is outside +-500
    assert len(result.curve_lag_ms) == 101
    assert json.loads(json.dumps(result.as_dict()))["config"]["sync_max_lag_ms"] == 500.0


def test_the_result_serialises_with_the_curve_and_the_windows() -> None:
    pts, server, t_b, browser = make_session(150, drift_ms_per_s=0.2, seed=8)
    d = estimate_sync(pts, server, t_b, browser, T_REC, SHORT_WINDOWS).as_dict()
    assert d["t_of_pts"]["b"] == pytest.approx(1.0002, abs=1e-4)
    assert len(d["curve"]["lag_ms"]) == len(d["curve"]["r"]) == 201
    assert {"pts_center", "lag_ms", "peak", "used", "residual_ms"} <= set(d["windows"][0])
    assert d["recorder_start_ms"] == T_REC
    json.dumps(d)  # plain JSON types only


def test_the_recorded_session_lines_up(tmp_path: Path, face_model: Path) -> None:
    """The browser's live model and the server's model, on the same webcam frames of a real
    recording (Chromium's fake camera playing the face clip)."""
    from tests.session_fixture import copy_fixture_session
    from worker.pipeline.features_a import FEATURES_FILE, compute_features
    from worker.pipeline.ingest import ingest
    from worker.pipeline.landmarks import detect_landmarks
    from worker.pipeline.sync import SYNC_FILE, run_sync

    session = copy_fixture_session(tmp_path)
    ingest(session.root, session.study_id, session.session_id, session.manifest, 32 * 1024 * 1024)
    derived = session.raw.parent / "derived"
    detect_landmarks(face_model, session.raw / "webcam.webm", derived / "landmarks.parquet")
    compute_features(derived / "landmarks.parquet", derived / FEATURES_FILE, CONFIG)

    result = run_sync(session.root, session.study_id, session.session_id, CONFIG)

    written = json.loads((derived / SYNC_FILE).read_text("utf-8"))
    assert written == json.loads(json.dumps(result.as_dict()))
    # The recording is 11 s: one window, slope 1. Both models see the very same frames, so the
    # signals are nearly identical (peak 0.998 and lag -4 ms when this was written) and the coarse
    # map from the recorder_start event is already right.
    assert result.flags == ["sync_short"]
    assert result.peak is not None and result.peak > 0.9
    assert abs(result.lag_ms) < 150
    assert result.b == 1.0
    assert result.frames["browser"] > 200 and result.frames["server"] > 200
