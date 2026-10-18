"""The debug replay video (PLAN §12.2): a real render with real ffmpeg, checked frame by frame."""

import logging
from pathlib import Path

import cv2
import numpy as np
import pyarrow.parquet as pq
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as DbSession
from typer.testing import CliRunner

import cli
from app.db import make_session_factory
from app.jobs import enqueue
from app.models import Job, JobKind, JobStatus, SessionStatus
from tests.dbutil import make_settings
from tests.session_db import seed_session
from tests.session_fixture import FixtureSession, copy_fixture_session
from tests.synthetic_session import (
    BLINK,
    FACE_LOST,
    VIDEO_FIRST_FRAME_MS,
    Synthetic,
    build,
)
from tests.videos import make_video, probe
from worker.handlers import make_render_debug
from worker.pipeline import replay
from worker.pipeline.config import PipelineConfig
from worker.pipeline.gaze import run_gaze
from worker.pipeline.replay import (
    BAR_HEIGHT,
    FINE,
    NO_FACE,
    STALLED,
    ReplayError,
    build_timeline,
    render_replay,
    timeline_bar,
)
from worker.pipeline.runner import Context, run_pipeline
from worker.run import process_one

CONFIG = PipelineConfig()
Colour = tuple[int, int, int]


def near(
    image: np.ndarray,
    centre: tuple[int, int],
    colour: Colour,
    radius: int = 12,
    tolerance: int = 90,
) -> bool:
    """Is a pixel of about this colour within `radius` px of `centre`? (H.264 shifts colours.)"""
    x, y = centre
    window = image[max(y - radius, 0) : y + radius + 1, max(x - radius, 0) : x + radius + 1].astype(
        int
    )
    distance = np.abs(window - np.array(colour)).sum(axis=2)
    return bool((distance < tolerance).any())


def frame_at(video: Path, index: int) -> np.ndarray:
    capture = cv2.VideoCapture(str(video))
    capture.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = capture.read()
    capture.release()
    assert ok
    return np.asarray(frame)


# --- on a session with a known scanpath ---


@pytest.fixture(scope="module")
def session(tmp_path_factory: pytest.TempPathFactory) -> Synthetic:
    s = build(tmp_path_factory.mktemp("replay"))
    run_gaze(s.root, s.study_id, s.session_id, CONFIG)
    make_video(s.root / "videos" / "1" / "video.mp4", seconds=8.4, size="800x600")
    return s


def derived_of(s: Synthetic) -> Path:
    return s.root / "studies" / "1" / "sessions" / str(s.session_id) / "derived"


@pytest.fixture(scope="module")
def rendered(session: Synthetic) -> replay.ReplaySummary:
    return render_replay(session.root, session.study_id, session.session_id, CONFIG)


def test_the_replay_is_a_playable_h264_video_as_long_as_the_playback(
    session: Synthetic, rendered: replay.ReplaySummary
) -> None:
    video = derived_of(session) / "debug" / "replay.mp4"
    info = probe(video)
    assert info["codec_name"] == "h264" and info["pix_fmt"] == "yuv420p"
    assert (info["width"], info["height"]) == ("960", str(720 + BAR_HEIGHT))
    assert info["r_frame_rate"] == "30/1"
    assert float(info["duration"]) == pytest.approx(8.0, abs=0.2)  # first stimulus frame to the end
    assert rendered.frames == pytest.approx(240, abs=3) and rendered.videos == ["1"]
    assert rendered.webcam_inset is False and rendered.bytes == video.stat().st_size
    # "faststart": the index is before the picture data, so a browser can start playing at once.
    head = video.read_bytes()[:4096]
    assert head.index(b"moov") < head.index(b"mdat") if b"mdat" in head else True
    assert not list(video.parent.glob(".*"))  # no temporary file left


def test_the_gaze_the_fixation_and_the_texts_are_drawn_where_the_data_says(
    session: Synthetic, rendered: replay.ReplaySummary
) -> None:
    gaze = pq.read_table(derived_of(session) / "gaze_a.parquet").to_pandas()
    t0 = float(gaze.loc[gaze["video_id"].notna(), "t"].iloc[0])
    t = 14_700.0  # inside the third fixation (700, 200)
    row = gaze.iloc[int((gaze["t"] - t).abs().argmin())]
    image = frame_at(derived_of(session) / "debug" / "replay.mp4", round((t - t0) / (1000 / 30)))

    def px(x: float, y: float) -> tuple[int, int]:
        return round(x * 959), round(y * 719)  # the 4:3 video fills the picture

    assert near(image, px(row["x_vid"], row["y_vid"]), replay.BLUE)  # smoothed
    assert near(image, px(row["x_vid_dc"], row["y_vid_dc"]), replay.GREEN)  # drift-corrected
    fixations = pq.read_table(derived_of(session) / "fixations_a.parquet").to_pandas()
    fix = fixations[(fixations["start_t"] <= t) & (fixations["end_t"] >= t)].iloc[0]
    radius = int(18 + min(fix["duration_ms"] / 20, 60))
    ring = (round(fix["cx_vid"] * 959) + radius, round(fix["cy_vid"] * 719))  # a point on the ring
    assert near(image, ring, replay.GREEN, radius=4)
    # The banner says what the file contains; it is red text in the top left corner.
    banner = image[5:25, 5:400].astype(int)
    assert ((banner[..., 2] > 180) & (banner[..., 1] < 130) & (banner[..., 0] < 130)).sum() > 30


def test_the_timeline_bar_shows_the_lost_face_and_the_fullscreen_exit(
    session: Synthetic, rendered: replay.ReplaySummary
) -> None:
    video = derived_of(session) / "debug" / "replay.mp4"
    tl = build_timeline(session.root, session.study_id, session.session_id, CONFIG)
    n, width = len(tl.times), 960

    def bar_pixel(t_ms: float, row: int) -> np.ndarray:
        frame = int(np.argmin(np.abs(tl.times - t_ms)))
        image = frame_at(video, 0)  # the bar's colours do not depend on the picture
        return np.asarray(image[720 + row, min(int(frame / n * width) + 2, width - 1)], dtype=int)

    lost = bar_pixel((FACE_LOST[0] + FACE_LOST[1]) / 2, 8)
    assert lost[2] > 150 and lost[1] < 120  # red (BGR)
    fine = bar_pixel(18_000.0, 8)
    assert abs(int(fine[0]) - int(fine[2])) < 40  # grey, not coloured
    tick = frame_at(video, 0)[720 + 20]
    assert any(abs(p.astype(int) - np.array(replay.PURPLE)).sum() < 90 for p in tick)  # the exit


def test_the_timeline_knows_what_happened(session: Synthetic) -> None:
    tl = build_timeline(session.root, session.study_id, session.session_id, CONFIG)
    assert tl.times[0] == pytest.approx(VIDEO_FIRST_FRAME_MS, abs=40)
    lost = (tl.times >= FACE_LOST[0]) & (tl.times < FACE_LOST[1])
    assert (tl.state[lost] == NO_FACE).all()
    blink = (tl.times >= BLINK[0]) & (tl.times < BLINK[1])
    assert (tl.state[blink] == FINE).all()  # an interpolated blink is not a problem to show
    assert (tl.state == STALLED).sum() == 0 and tl.boundaries == [] and len(tl.ticks) == 1
    assert abs(tl.times[tl.ticks[0]] - 16_050) < 40 and len(tl.fixations) == 5


def test_a_paused_video_stands_still_and_shows_as_stalled(session: Synthetic) -> None:
    tl = build_timeline(session.root, session.study_id, session.session_id, CONFIG)
    tl.stim[100:130] = np.nan
    tl.state[100:130] = STALLED
    bar = timeline_bar(960, tl)
    middle = bar[8, int(115 / len(tl.times) * 960)]
    assert tuple(int(c) for c in middle) == replay.ORANGE


# --- errors ---


def test_without_gaze_or_video_the_replay_says_what_is_missing(tmp_path: Path) -> None:
    s = build(tmp_path)  # the gaze stage has not run
    with pytest.raises(ReplayError, match="no gaze for this session"):
        render_replay(s.root, s.study_id, s.session_id, CONFIG)
    run_gaze(s.root, s.study_id, s.session_id, CONFIG)
    with pytest.raises(ReplayError, match="stimulus video 1 is not on this machine"):
        render_replay(s.root, s.study_id, s.session_id, CONFIG)
    debug = derived_of(s) / "debug"
    assert not debug.exists() or not list(debug.glob("*replay*"))  # nothing half written


# --- with the webcam, on the recorded session ---


@pytest.fixture(scope="module")
def recorded(tmp_path_factory: pytest.TempPathFactory, face_model: Path) -> FixtureSession:
    s = copy_fixture_session(tmp_path_factory.mktemp("recorded"))
    run_pipeline(
        Context(s.root, s.study_id, s.session_id, CONFIG, face_model, 32 * 1024 * 1024, s.manifest)
    )
    make_video(s.root / "videos" / "1" / "video.mp4", seconds=3.0)
    return s


def test_the_webcam_with_landmarks_is_in_the_corner(recorded: FixtureSession) -> None:
    summary = render_replay(recorded.root, recorded.study_id, recorded.session_id, CONFIG)
    video = recorded.raw.parent / "derived" / "debug" / "replay.mp4"
    assert summary.webcam_inset and summary.seconds == pytest.approx(3.0, abs=0.3)
    image = frame_at(video, 30)
    inset = image[720 - 240 : 720 - 8, 960 - 240 : 960 - 8].astype(int)  # the bottom right corner
    assert inset.std() > 25  # a face, not a flat patch
    assert near(image, (960 - 120, 720 - 120), replay.CYAN, radius=110, tolerance=120) or near(
        image, (960 - 120, 720 - 120), replay.YELLOW, radius=110, tolerance=120
    )  # landmark dots


def test_the_replay_of_the_recorded_session_is_made_by_the_job_and_the_cli(
    recorded: FixtureSession, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_session(migrated_engine, recorded, SessionStatus.PROCESSED)
    settings = make_settings(data_root=recorded.root)
    factory = make_session_factory(migrated_engine)
    video = recorded.raw.parent / "derived" / "debug" / "replay.mp4"
    video.unlink(missing_ok=True)

    with factory() as db:
        job = enqueue(db, JobKind.RENDER_DEBUG, {"session_id": str(recorded.session_id)})
        db.commit()
        job_id = job.id
    assert (
        process_one(factory, {JobKind.RENDER_DEBUG: make_render_debug(factory, settings)}) is True
    )
    with DbSession(migrated_engine) as db:
        job = db.get_one(Job, job_id)
        assert job.status is JobStatus.DONE and job.log.startswith("replay rendered: ")
    assert video.is_file()

    video.unlink()
    monkeypatch.setattr(cli, "settings", lambda: settings)
    monkeypatch.setattr(cli, "open_session", lambda: factory())
    monkeypatch.setattr(cli, "log_level", lambda: "INFO")
    result = CliRunner().invoke(cli.app, ["render-debug", str(recorded.session_id)])
    logging.getLogger().handlers.clear()
    assert result.exit_code == 0, result.output
    assert "Wrote" in result.output and "participant's webcam video" in result.output
    assert video.is_file()


def test_a_failed_render_fails_the_job_with_the_reason(
    tmp_path: Path, migrated_engine: Engine
) -> None:
    s = build(tmp_path / "data")  # no gaze
    seed_session(
        migrated_engine,
        FixtureSession(s.root, s.study_id, s.session_id, [], s.root),
        SessionStatus.PROCESSED,
    )
    factory = make_session_factory(migrated_engine)
    with factory() as db:
        enqueue(db, JobKind.RENDER_DEBUG, {"session_id": str(s.session_id)})
        enqueue(db, JobKind.RENDER_DEBUG, {})
        db.commit()
    handler = make_render_debug(factory, make_settings(data_root=s.root))
    assert process_one(factory, {JobKind.RENDER_DEBUG: handler}) is True
    assert process_one(factory, {JobKind.RENDER_DEBUG: handler}) is True
    with DbSession(migrated_engine) as db:
        logs = [j.log for j in db.query(Job).order_by(Job.id)]
        assert all(j.status is JobStatus.FAILED for j in db.query(Job))
    assert "no gaze for this session" in logs[0] and "needs a session_id" in logs[1]
