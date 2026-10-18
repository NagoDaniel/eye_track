"""The quality report and the inclusion decision (PLAN §8.10, §4.1)."""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.schemas import EventRecord, InclusionConfig
from app.storage import meta_path
from tests.synthetic_session import (
    BLINK,
    FACE_LOST,
    VIDEO_END_MS,
    VIDEO_FIRST_FRAME_MS,
    Synthetic,
    build,
)
from worker.pipeline.config import PipelineConfig
from worker.pipeline.gaze import run_gaze
from worker.pipeline.quality import (
    QUALITY_FILE,
    Inclusion,
    build_quality,
    decide_inclusion,
    event_counts,
    face_lost,
    webcam_stats,
)

CONFIG = PipelineConfig()
INCLUSION = InclusionConfig(max_validation_error_norm=0.12, min_valid_fraction=0.7)


def inclusion(**kw: object) -> Inclusion:
    args: dict[str, object] = {
        "is_debug": False, "calibration_ok": True, "playback_frames": 300, "valid_fraction": 0.9,
        "validation_error_norm": 0.05, "inclusion": INCLUSION,
    }  # fmt: skip
    args.update(kw)
    return decide_inclusion(**args)  # type: ignore[arg-type]


# --- inclusion ---


def test_a_good_session_is_included() -> None:
    result = inclusion()
    assert result.included and result.reasons == []
    assert result.criteria["max_validation_error_norm"] == 0.12


def test_each_failure_gives_its_reason_and_all_are_listed() -> None:
    assert inclusion(validation_error_norm=0.2).reasons == ["validation_error_high"]
    assert inclusion(valid_fraction=0.5).reasons == ["valid_fraction_low"]
    assert inclusion(is_debug=True).reasons == ["debug_session"]
    assert inclusion(playback_frames=0, valid_fraction=None).reasons == ["no_playback_data"]
    assert inclusion(validation_error_norm=None).reasons == ["no_validation_data"]
    both = inclusion(validation_error_norm=0.2, valid_fraction=0.5, is_debug=True)
    assert both.reasons == ["debug_session", "valid_fraction_low", "validation_error_high"]
    assert not both.included


def test_a_value_equal_to_its_threshold_passes() -> None:
    assert inclusion(validation_error_norm=0.12, valid_fraction=0.7).included


def test_a_failed_calibration_excludes_without_judging_the_rest() -> None:
    result = inclusion(calibration_ok=False, playback_frames=0, valid_fraction=None,
                       validation_error_norm=None)  # fmt: skip
    assert result.reasons == ["calibration_failed"]


def test_the_thresholds_are_the_studys() -> None:
    strict = InclusionConfig(max_validation_error_norm=0.01, min_valid_fraction=0.95)
    assert inclusion(inclusion=strict).reasons == ["valid_fraction_low", "validation_error_high"]


# --- pieces ---


def test_webcam_stats() -> None:
    pts = np.arange(0, 3.0, 1 / 30)
    pts = np.delete(pts, [30, 31, 32, 33])  # a gap of 5 frames (167 ms)
    stats = webcam_stats(pts, 100.0)
    assert stats["frames"] == len(pts) and stats["gaps"] == 1
    assert stats["max_gap_ms"] == pytest.approx(5000 / 30)
    assert stats["fps_median"] == pytest.approx(30.0)
    assert 26 < stats["fps_mean"] < 30
    assert webcam_stats(np.array([0.0]), 100.0)["fps_mean"] is None


def test_face_lost_episodes_last_from_the_first_frame_without_a_face() -> None:
    t = np.arange(60) * 100.0
    face = np.ones(60, bool)
    face[10:16] = False  # 600 ms: the first frame without a face to the first with one
    face[30:33] = False  # 300 ms: too short
    face[50:] = False  # lasts to the end of the recording
    result = face_lost(face, t, 500.0)
    assert result["episodes"] == 2 and result["longest_ms"] == pytest.approx(1000.0)
    assert result["total_ms"] == pytest.approx(600.0 + 1000.0)
    assert face_lost(face, t, 250.0)["episodes"] == 3
    within = np.zeros(60, bool)
    within[8:40] = True  # only this stretch counts, and a stretch ends where it ends
    assert face_lost(face, t, 250.0, within)["episodes"] == 2
    assert face_lost(np.ones(5, bool), np.arange(5.0), 1.0)["episodes"] == 0


def ev(t: float, type_: str, **data: object) -> EventRecord:
    return EventRecord.model_validate({"t": t, "type": type_, "data": data})


def test_event_counts_cover_the_recording_and_the_playback() -> None:
    events = [
        ev(0, "fullscreen_change", fullscreen=False),  # before the recording: not counted
        ev(100, "recorder_start", mime_type="x", bits_per_second=1, settings={}),
        ev(200, "fullscreen_change", fullscreen=True),  # entering is not an exit
        ev(300, "fullscreen_change", fullscreen=False),
        ev(1000, "video_load", video_id="1"),
        ev(1000, "video_stall", video_id="1", media_time=0),
        ev(1500, "video_stall", video_id="1", media_time=0.4),
        ev(1600, "fullscreen_change", fullscreen=False),
        ev(2000, "video_ended", video_id="1"),
        ev(2100, "visibility_change", state="hidden"),
        ev(2150, "visibility_change", state="visible"),
        ev(2200, "window_blur"),
        ev(3000, "recorder_stop", chunks=1, bytes=1),
        ev(4000, "window_blur"),  # after the recording
        ev(900, "face_lost"),
    ]
    counts = event_counts(sorted(events, key=lambda e: e.t))
    assert counts["fullscreen_exits"] == 2 and counts["fullscreen_exits_in_playback"] == 1
    assert counts["stalls"] == 1 and counts["stalls_at_start"] == 1
    assert counts["page_left"] == 2
    assert counts["browser_face_lost_episodes"] == 1


# --- the report on a session with a known history ---


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Synthetic:
    s = build(tmp_path_factory.mktemp("quality"))
    run_gaze(s.root, s.study_id, s.session_id, CONFIG)
    return s


@pytest.fixture(scope="module")
def report(session: Synthetic) -> dict[str, Any]:
    stages = {"ingest": {"seconds": 1.5, "summary": {"frames": 3}}}
    return build_quality(session.root, session.study_id, session.session_id, CONFIG, stages)


def derived_of(s: Synthetic) -> Path:
    return s.root / "studies" / "1" / "sessions" / str(s.session_id) / "derived"


def test_the_report_is_written_and_is_plain_json(
    session: Synthetic, report: dict[str, Any]
) -> None:
    written = json.loads((derived_of(session) / QUALITY_FILE).read_text("utf-8"))
    assert written == json.loads(json.dumps(report))
    assert written["stages"]["ingest"]["seconds"] == 1.5
    assert written["version"] == 1 and written["is_debug"] is False


def test_playback_valid_fraction_matches_the_designed_history(
    session: Synthetic, report: dict[str, Any]
) -> None:
    t = session.t
    playing = (t >= VIDEO_FIRST_FRAME_MS) & (t <= VIDEO_END_MS)
    bad = ((t >= BLINK[0]) & (t < BLINK[1])) | ((t >= FACE_LOST[0]) & (t < FACE_LOST[1]))
    playback = report["playback"]
    assert playback["frames"] == int(playing.sum())
    expected = 1 - (bad & playing).sum() / playing.sum()
    assert playback["valid_fraction"] == pytest.approx(expected)
    assert playback["per_video"]["1"]["frames"] == playback["frames"]
    assert playback["blink_fraction"] == pytest.approx(3 / playing.sum(), abs=0.002)
    assert playback["interpolated_fraction"] == pytest.approx(3 / playing.sum(), abs=0.002)


def test_webcam_events_and_gate_in_the_report(report: dict[str, Any]) -> None:
    assert report["webcam"]["fps_mean"] == pytest.approx(30.0, abs=0.1)
    assert report["webcam"]["gaps"] == 0
    events = report["events"]
    assert events["fullscreen_exits"] == 1 and events["fullscreen_exits_in_playback"] == 1
    assert events["stalls"] == 1 and events["stalls_at_start"] == 1
    gate = report["browser_gate"]
    assert len(gate) == 1 and gate[0]["passed"] is True and gate[0]["mean_error_norm"] == 0.04
    assert report["calibration"]["attempts"][0]["passed"] is True
    assert report["calibration"]["cv"]["chosen"] == "linear"
    assert report["sync"]["flags"] == []


def test_face_lost_uses_the_browsers_500_ms_and_the_config(session: Synthetic) -> None:
    # The designed loss is 400 ms: no episode at 500 ms, one at 300 ms.
    default = build_quality(session.root, session.study_id, session.session_id, CONFIG)
    assert default["face_lost"]["all"]["episodes"] == 0
    sensitive = replace(CONFIG, quality_face_lost_min_ms=300.0)
    result = build_quality(session.root, session.study_id, session.session_id, sensitive)
    assert result["face_lost"]["all"]["episodes"] == 1
    assert result["face_lost"]["all"]["total_ms"] == pytest.approx(400, abs=40)
    assert result["face_lost"]["playback"]["episodes"] == 1
    build_quality(session.root, session.study_id, session.session_id, CONFIG)  # restore the file


def test_validation_drift_and_face_size_are_reported(report: dict[str, Any]) -> None:
    validation = report["validation"]
    assert [b["kind"] for b in validation["blocks"]] == ["before_video", "after_last_video"]
    assert 0 < validation["mean_error_norm"] < 0.05
    assert validation["mean_error_corrected_norm"] < validation["mean_error_norm"]
    assert validation["drift"] is not None and len(validation["drift"]["t_ms"]) == 2
    ratio = report["face_ratio"]
    assert ratio["calibration_median"] == pytest.approx(0.3)
    assert ratio["playback_share_outside"] == 0.0


def test_the_designed_session_is_included(report: dict[str, Any]) -> None:
    assert report["inclusion"]["included"] is True and report["inclusion"]["reasons"] == []


def test_the_studys_thresholds_and_the_debug_flag_come_from_the_session_files(
    tmp_path: Path,
) -> None:
    s = build(tmp_path)
    run_gaze(s.root, s.study_id, s.session_id, CONFIG)
    path = meta_path(s.root, s.study_id, s.session_id)
    meta = json.loads(path.read_text())
    meta["config"]["inclusion"] = {"max_validation_error_norm": 0.001, "min_valid_fraction": 0.99}
    meta["is_debug"] = True
    path.write_text(json.dumps(meta))
    report = build_quality(s.root, s.study_id, s.session_id, CONFIG)
    assert report["inclusion"]["reasons"] == [
        "debug_session", "valid_fraction_low", "validation_error_high",
    ]  # fmt: skip
    assert report["is_debug"] is True


def test_a_failed_calibration_gives_a_report_without_gaze(tmp_path: Path) -> None:
    s = build(tmp_path)  # gaze stage not run: there is none for a failed calibration
    path = derived_of(s) / "calibration_a.json"
    path.write_text(json.dumps({"status": "failed", "failed_reason": "too_few_valid_frames",
                                "attempts": [], "cv": None, "samples": None}))  # fmt: skip
    report = build_quality(s.root, s.study_id, s.session_id, CONFIG)
    assert report["playback"] is None and report["validation"] is None
    assert report["calibration"]["failed_reason"] == "too_few_valid_frames"
    assert report["inclusion"]["included"] is False
    assert report["inclusion"]["reasons"] == ["calibration_failed"]
    assert report["webcam"]["frames"] > 100 and report["events"]["fullscreen_exits"] == 1


def test_the_recorded_session_has_a_complete_report(tmp_path: Path, face_model: Path) -> None:
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
    run_gaze(s.root, s.study_id, s.session_id, CONFIG)

    report = build_quality(s.root, s.study_id, s.session_id, CONFIG)

    assert report["session_flags"] == ["calibration_gate_failed"]
    assert report["webcam"]["fps_mean"] == pytest.approx(30, abs=3)
    assert report["calibration"]["status"] == "ok"
    assert [g["block"] for g in report["browser_gate"]] == ["before_video", "after_last_video"]
    assert report["sync"]["flags"] == ["sync_short"]
    assert report["playback"]["frames"] > 50
    assert report["events"]["stalls_at_start"] == 1
    assert report["validation"]["blocks"][0]["kind"] == "before_video"
    # The fake camera does not look at the dots, so the session fails the study's own thresholds.
    assert report["inclusion"]["included"] is False and report["inclusion"]["reasons"]
    json.dumps(report)
