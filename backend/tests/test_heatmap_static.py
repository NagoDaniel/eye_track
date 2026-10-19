"""Static heatmaps per segment on the real moving-dot video (PLAN §9.2, §13)."""

from pathlib import Path

import cv2
import numpy as np
import pytest

from app.aois import Segment
from tests.test_heatmap_video import DOT, PARTICIPANTS, SIGMA, VIDEO
from worker.analysis.config import AnalysisConfig
from worker.analysis.heatmap_static import file_name, render_static, segments_for

CONFIG = AnalysisConfig()


def picture(path: Path) -> np.ndarray:
    image = cv2.imread(str(path))
    assert image is not None
    return np.asarray(image)


def test_default_windows_cover_the_video_with_a_shorter_last_one() -> None:
    windows = segments_for([], 21.8, CONFIG)
    assert [(w.start, round(w.end, 3)) for w in windows] == [(0, 10), (10, 20), (20, 21.8)]
    assert [w.id for w in windows] == ["window_1", "window_2", "window_3"]
    assert [(w.start, w.end) for w in segments_for([], 10.0, CONFIG)] == [(0, 10)]
    assert [w.end for w in segments_for([], 4.0, AnalysisConfig(segment_default_s=1.5))] == [
        1.5, 3.0, 4.0
    ]  # fmt: skip


def test_configured_segments_replace_the_defaults_and_are_cut_to_the_video() -> None:
    given = [Segment(id="a", start=1, end=3), Segment(id="b", start=20, end=40),
             Segment(id="late", start=30, end=35)]  # fmt: skip
    chosen = segments_for(given, 21.8, CONFIG)
    assert [(s.id, s.start, s.end) for s in chosen] == [("a", 1, 3), ("b", 20, 21.8)]


def test_file_names_are_safe_and_ordered() -> None:
    assert file_name(0, "intro") == "static/001_intro.png"
    assert file_name(11, "../../etc/x y") == "static/012_etc_x_y.png"
    assert file_name(2, "///") == "static/003_segment.png"


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[str]]:
    run_dir = tmp_path_factory.mktemp("static")
    segments = [Segment(id="rest", start=2.9, end=4.0), Segment(id="far", start=7.0, end=8.0)]
    summary = render_static(VIDEO, run_dir, PARTICIPANTS, SIGMA, segments, CONFIG)
    return run_dir, summary.files


def test_one_valid_picture_per_segment_in_the_size_of_the_video(
    rendered: tuple[Path, list[str]],
) -> None:
    run_dir, files = rendered
    assert files == ["static/001_rest.png", "static/002_far.png"]
    for name in files:
        assert picture(run_dir / name).shape == (720, 1280, 3)
    assert not list((run_dir / "static").glob("*.tmp"))


def test_the_heat_is_where_the_dot_was_during_the_segment(
    rendered: tuple[Path, list[str]],
) -> None:
    run_dir, files = rendered
    h, w = 720, 1280
    yy, xx = np.mgrid[0:h, 0:w]
    # 2.9 to 4.0 s: the dot rests at (320, 180) from 2.7 s to 4.2 s
    rest = picture(run_dir / files[0]).astype(np.int16)
    reference = _frame_at(3.45)
    diff = np.abs(rest - reference).mean(axis=2)
    near = np.hypot((xx - 320) / w, (yy - 180) / h) < 0.05
    far = np.hypot((xx - 960) / w, (yy - 540) / h) < 0.15
    assert diff[near].mean() > 20 and diff[far].mean() < 2
    x, y = DOT.at(np.array([3.45]))
    assert (round(x[0] * w), round(y[0] * h)) == (320, 180)  # the path file agrees


def _frame_at(seconds: float) -> np.ndarray:
    capture = cv2.VideoCapture(str(VIDEO))
    capture.set(cv2.CAP_PROP_POS_FRAMES, round(seconds * 25))
    ok, frame = capture.read()
    assert ok
    return np.asarray(frame).astype(np.int16)


def test_a_segment_without_samples_is_the_plain_frame(tmp_path: Path) -> None:
    summary = render_static(
        VIDEO, tmp_path, PARTICIPANTS, SIGMA, [Segment(id="after", start=30, end=31)], CONFIG
    )
    assert summary.segments[0]["samples"] == 0
    assert (
        np.abs(picture(tmp_path / summary.files[0]).astype(np.int16) - _frame_at(21.7)).mean() < 2
    )


def test_the_summary_counts_participants_and_samples(tmp_path: Path) -> None:
    summary = render_static(
        VIDEO, tmp_path, PARTICIPANTS, SIGMA, [Segment(id="s", start=3, end=4)], CONFIG
    )
    info = summary.segments[0]
    assert info["participants_with_samples"] == 3 and info["file"] == "static/001_s.png"
    assert 80 <= int(info["samples"]) <= 93  # three participants, 30 samples a second, 1 s
