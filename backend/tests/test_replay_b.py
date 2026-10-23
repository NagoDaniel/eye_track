"""The debug replay with Pipeline B's gaze in it (PLAN §12.2): real ffmpeg, the recorded session
after A and B (the real small gaze network), a plain grey stimulus so no colour of the video can be
mistaken for a gaze dot. No database."""

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np
import pytest

from app.storage import derived_dir
from tests.session_fixture import FixtureSession
from tests.test_replay import near
from worker.pipeline import replay
from worker.pipeline.config import PipelineConfig
from worker.pipeline.replay import (
    B_CORRECTED,
    B_RAW,
    B_SMOOTH,
    BAR_HEIGHT,
    FrameReader,
    PipelineDots,
    Timeline,
    build_timeline,
    draw_frame,
    render_replay,
    timeline_bar,
)
from worker.pipeline.spec import SPEC_B

CONFIG = PipelineConfig()
WIDTH, HEIGHT = 960, 720  # the 4:3 stimulus fills the picture of replay_width 960
GREY_BGR = 0x60


def grey_video(path: Path, seconds: float = 3.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
         f"color=c=0x606060:size=320x240:rate=25:duration={seconds}",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", str(path)],
        check=True,
    )  # fmt: skip


def copy_session(b_session: FixtureSession, target: Path) -> FixtureSession:
    shutil.copytree(b_session.root, target)
    grey_video(target / "videos" / "1" / "video.mp4")
    return FixtureSession(
        target, b_session.study_id, b_session.session_id, b_session.manifest,
        target / b_session.raw.relative_to(b_session.root),
    )  # fmt: skip


@pytest.fixture(scope="module")
def with_b(b_session: FixtureSession, tmp_path_factory: pytest.TempPathFactory) -> FixtureSession:
    return copy_session(b_session, tmp_path_factory.mktemp("replay_with_b") / "data")


@pytest.fixture(scope="module")
def without_b(
    b_session: FixtureSession, tmp_path_factory: pytest.TempPathFactory
) -> FixtureSession:
    """The same session with B's gaze files taken away: what the replay was before Pipeline B."""
    s = copy_session(b_session, tmp_path_factory.mktemp("replay_without_b") / "data")
    derived = derived_dir(s.root, s.study_id, s.session_id)
    (derived / SPEC_B.gaze_file).unlink()
    (derived / SPEC_B.fixations_file).unlink()
    return s


def pixel(point: tuple[float, float]) -> tuple[int, int]:
    return round(point[0] * (WIDTH - 1)), round(point[1] * (HEIGHT - 1))


def clear_of_the_overlays(x: int, y: int) -> bool:
    """Away from the text panel (top left), the webcam inset (bottom right) and the picture edge."""
    in_panel = x < replay.TEXT_PANEL_WIDTH + replay.B_LEGEND_WIDTH_EXTRA + 40 and y < 160
    in_inset = x > WIDTH - 300 and y > HEIGHT - 300
    return not in_panel and not in_inset and 40 < x < WIDTH - 40 and 40 < y < HEIGHT - 40


def frame_with_b_dots(tl: Timeline) -> int:
    """A frame in which B's three dots are on the video, apart from each other and from A's dots,
    and clear of the overlays."""
    assert tl.pipeline_b is not None
    b = tl.pipeline_b
    for k in range(len(tl.times)):
        dots = [(b.raw[0][k], b.raw[1][k]), (b.smooth[0][k], b.smooth[1][k]),
                (b.corrected[0][k], b.corrected[1][k])]  # fmt: skip
        others = [(tl.raw[0][k], tl.raw[1][k]), (tl.smooth[0][k], tl.smooth[1][k]),
                  (tl.corrected[0][k], tl.corrected[1][k])]  # fmt: skip
        if not all(np.isfinite(p).all() and 0 <= p[0] <= 1 and 0 <= p[1] <= 1 for p in dots):
            continue
        pixels = [pixel(p) for p in dots]
        if not all(clear_of_the_overlays(*p) for p in pixels):
            continue
        near_a = [
            np.hypot(p[0] - q[0], p[1] - q[1]) < 30
            for p in pixels
            for q in (pixel(o) for o in others if np.isfinite(o).all())
        ]
        near_b = [np.hypot(p[0] - q[0], p[1] - q[1]) < 30 for p in pixels for q in pixels if p != q]
        if not any(near_a) and not any(near_b):
            return k
    pytest.fail("no frame has B's dots apart from A's and clear of the overlays")


def frame_of(video: Path, index: int) -> np.ndarray:
    capture = cv2.VideoCapture(str(video))
    capture.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = capture.read()
    capture.release()
    assert ok
    return np.asarray(frame)


# --- the film ---


def test_the_replay_with_b_has_the_frames_and_the_size_of_the_replay_without(
    with_b: FixtureSession, without_b: FixtureSession
) -> None:
    with_summary = render_replay(with_b.root, with_b.study_id, with_b.session_id, CONFIG)
    without_summary = render_replay(
        without_b.root, without_b.study_id, without_b.session_id, CONFIG
    )
    assert with_summary.pipeline_b is True and without_summary.pipeline_b is False
    assert with_summary.as_dict()["pipeline_b"] is True
    assert with_summary.frames == without_summary.frames > 50
    assert (with_summary.width, with_summary.height) == (WIDTH, HEIGHT + BAR_HEIGHT)
    assert (with_summary.width, with_summary.height) == (
        without_summary.width,
        without_summary.height,
    )
    assert with_summary.seconds == without_summary.seconds and with_summary.videos == ["1"]
    assert with_summary.webcam_inset and without_summary.webcam_inset  # the webcam is still there
    for s in (with_b, without_b):
        video = derived_dir(s.root, s.study_id, s.session_id) / "debug" / "replay.mp4"
        capture = cv2.VideoCapture(str(video))
        assert int(capture.get(cv2.CAP_PROP_FRAME_COUNT)) == with_summary.frames
        assert int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)) == WIDTH
        assert int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) == HEIGHT + BAR_HEIGHT
        capture.release()


def test_the_timeline_has_b_only_when_b_has_gaze(
    with_b: FixtureSession, without_b: FixtureSession
) -> None:
    tl = build_timeline(with_b.root, with_b.study_id, with_b.session_id, CONFIG)
    assert isinstance(tl.pipeline_b, PipelineDots)
    assert len(tl.pipeline_b.smooth[0]) == len(tl.times)
    assert np.isfinite(tl.pipeline_b.smooth[0]).any()
    # B's gaze is its own: the dots are not A's.
    both = np.isfinite(tl.pipeline_b.smooth[0]) & np.isfinite(tl.smooth[0])
    assert both.any() and not np.allclose(tl.pipeline_b.smooth[0][both], tl.smooth[0][both])
    assert (
        build_timeline(without_b.root, without_b.study_id, without_b.session_id, CONFIG).pipeline_b
        is None
    )


def test_a_frame_with_b_has_b_s_colours_at_b_s_gaze_and_one_without_b_has_none(
    with_b: FixtureSession, without_b: FixtureSession
) -> None:
    """In the encoded films (H.264 shifts colours, hence the tolerance)."""
    render_replay(with_b.root, with_b.study_id, with_b.session_id, CONFIG)
    render_replay(without_b.root, without_b.study_id, without_b.session_id, CONFIG)
    tl = build_timeline(with_b.root, with_b.study_id, with_b.session_id, CONFIG)
    assert tl.pipeline_b is not None
    k = frame_with_b_dots(tl)
    film_b = derived_dir(with_b.root, with_b.study_id, with_b.session_id) / "debug" / "replay.mp4"
    film_a = (
        derived_dir(without_b.root, without_b.study_id, without_b.session_id)
        / "debug"
        / "replay.mp4"
    )
    image_b, image_a = frame_of(film_b, k), frame_of(film_a, k)
    b = tl.pipeline_b
    for colour, dot in ((B_RAW, b.raw), (B_SMOOTH, b.smooth)):
        centre = pixel((float(dot[0][k]), float(dot[1][k])))
        assert near(image_b, centre, colour, radius=8, tolerance=90), (colour, centre)
        assert not near(image_a, centre, colour, radius=8, tolerance=90), (colour, centre)
    # nowhere in the picture of the film without B is there a magenta or cyan pixel
    picture = image_a[:HEIGHT].astype(int)
    for colour in (B_RAW, B_SMOOTH):
        assert (np.abs(picture - np.array(colour)).sum(axis=2) < 60).sum() == 0, colour


def test_a_drawn_frame_differs_only_where_b_is_drawn(with_b: FixtureSession) -> None:
    """Exact pixels, no encoder: with B's dots, ring and legend added, every other pixel of the
    frame (A's dots, A's ring, the webcam, the bar) is the one of the frame without B."""
    tl = build_timeline(with_b.root, with_b.study_id, with_b.session_id, CONFIG)
    assert tl.pipeline_b is not None
    k = frame_with_b_dots(tl)
    picture = np.full((240, 320, 3), GREY_BGR, dtype=np.uint8)
    inset = np.full((90, 240, 3), 20, dtype=np.uint8)
    bar = timeline_bar(WIDTH, tl)
    size = (WIDTH, HEIGHT)
    with_dots = draw_frame(k, tl, picture, size, bar, [], inset)
    plain = draw_frame(k, replace(tl, pipeline_b=None), picture, size, bar, [], inset)
    assert with_dots.shape == plain.shape == (HEIGHT + BAR_HEIGHT, WIDTH, 3)

    changed = np.any(with_dots != plain, axis=2)
    b = tl.pipeline_b
    allowed = np.zeros_like(changed)
    allowed[: replay.TEXT_PANEL_HEIGHT + replay.B_LEGEND_HEIGHT_EXTRA,
            : replay.TEXT_PANEL_WIDTH + replay.B_LEGEND_WIDTH_EXTRA] = True  # fmt: skip
    for dot in (b.raw, b.smooth, b.corrected):
        x, y = pixel((float(dot[0][k]), float(dot[1][k])))
        allowed[max(y - 10, 0) : y + 11, max(x - 10, 0) : x + 11] = True
    here = np.flatnonzero((b.fixations[:, 0] <= tl.times[k]) & (tl.times[k] <= b.fixations[:, 1]))
    for f in here[:1]:  # a fixation ring of B: its bounding box
        centre = (float(b.fixations[f, 3]), float(b.fixations[f, 4]))
        x, y = replay.to_pixels(centre, (0, 0, WIDTH, HEIGHT))  # clipped to the picture
        r = int(18 + min(b.fixations[f, 2] / 20, 60)) + replay.B_RING_EXTRA + 3
        if not (0 <= centre[0] <= 1 and 0 <= centre[1] <= 1):
            r *= 3  # off the video the ring is a cross three times as large
        allowed[max(y - r, 0) : y + r + 1, max(x - r, 0) : x + r + 1] = True
    assert changed.any() and not (changed & ~allowed).any()

    # the exact colours are at the centre of B's squares, and the plain frame has none of them
    for colour, dot in ((B_RAW, b.raw), (B_SMOOTH, b.smooth), (B_CORRECTED, b.corrected)):
        x, y = pixel((float(dot[0][k]), float(dot[1][k])))
        assert tuple(with_dots[y, x]) == colour
        window = plain[max(y - 12, 0) : y + 13, max(x - 12, 0) : x + 13]
        assert not np.all(window == np.array(colour), axis=2).any()


def test_the_legend_names_both_pipelines_only_when_b_is_drawn(with_b: FixtureSession) -> None:
    tl = build_timeline(with_b.root, with_b.study_id, with_b.session_id, CONFIG)
    picture = np.full((240, 320, 3), GREY_BGR, dtype=np.uint8)
    bar = timeline_bar(WIDTH, tl)
    with_dots = draw_frame(0, tl, picture, (WIDTH, HEIGHT), bar, [], None)
    plain = draw_frame(0, replace(tl, pipeline_b=None), picture, (WIDTH, HEIGHT), bar, [], None)
    # the third text line is the A legend; with B a fourth line (y 78) is the B legend
    line_b = (slice(66, 84), slice(0, 560))
    assert (with_dots[line_b] != plain[line_b]).any()
    assert with_dots[line_b].std() > plain[line_b].std()  # text where there was only the dark panel


def test_the_video_is_decoded_once(with_b: FixtureSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """B adds dots to the same pass: the stimulus video is opened once, not once per pipeline."""
    opened: list[str] = []
    original = FrameReader.__init__

    def counting(self: FrameReader, path: Path) -> None:
        opened.append(path.name)
        original(self, path)

    monkeypatch.setattr(FrameReader, "__init__", counting)
    render_replay(with_b.root, with_b.study_id, with_b.session_id, CONFIG)
    assert opened.count("video.mp4") == 1
