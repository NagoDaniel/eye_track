"""The dynamic heatmap video on the real moving-dot video (PLAN §9.1, §13).

Real ffmpeg and OpenCV: three simulated participants follow the dot's known path (with their own
offsets and noise), so the heat in every frame must sit on the dot.
"""

import json
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

from worker.analysis.config import AnalysisConfig
from worker.analysis.density import StimSamples, grid_axes
from worker.analysis.heatmap_video import (
    DENSITY_FILE,
    HEATMAP_VIDEO_FILE,
    HeatmapSummary,
    colour_scale,
    draw_dots,
    overlay,
    render_heatmap,
)
from worker.pipeline.moving_dot import DotPath

FIXTURES = Path(__file__).resolve().parents[2] / "e2e" / "fixtures"
VIDEO = FIXTURES / "moving_dot.mp4"
DOT = DotPath.load(FIXTURES / "moving_dot_path.json")
CONFIG = AnalysisConfig()
SIGMA = 0.05
ENCODING_NOISE = 6  # mean grey-level difference two H.264 encodes of one picture may show
PAUSE_S = 3.5  # the dot rests at (320, 180) of 1280 x 720 from 2.7 s to 4.2 s


def follower(offset: tuple[float, float], seed: int, noise: float = 0.01) -> StimSamples:
    """A participant looking at the dot (plus an offset and noise), 30 samples per second."""
    rng = np.random.default_rng(seed)
    s = np.arange(0.0, DOT.duration_s, 1 / 30)
    x, y = DOT.at(s)
    return StimSamples(
        s,
        x + offset[0] + rng.normal(0, noise, len(s)),
        y + offset[1] + rng.normal(0, noise, len(s)),
    )


PARTICIPANTS = [follower((0.0, 0.0), 1), follower((0.02, -0.01), 2), follower((-0.015, 0.02), 3)]


def probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,pix_fmt,width,height,avg_frame_rate,nb_read_frames", "-of", "json",
         str(path)],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    stream: dict[str, Any] = json.loads(out.stdout)["streams"][0]
    return stream


def frame_at(path: Path, index: int) -> np.ndarray:
    capture = cv2.VideoCapture(str(path))
    capture.set(cv2.CAP_PROP_POS_FRAMES, index)
    ok, frame = capture.read()
    capture.release()
    assert ok
    return np.asarray(frame)


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, HeatmapSummary]:
    run_dir = tmp_path_factory.mktemp("heatmap")
    return run_dir, render_heatmap(VIDEO, run_dir, PARTICIPANTS, SIGMA, CONFIG)


def test_the_video_has_the_stimulus_frames_size_and_rate(
    rendered: tuple[Path, HeatmapSummary],
) -> None:
    run_dir, summary = rendered
    info = probe(run_dir / HEATMAP_VIDEO_FILE)
    source = probe(VIDEO)
    assert info["codec_name"] == "h264" and info["pix_fmt"] == "yuv420p"
    assert int(info["nb_read_frames"]) == int(source["nb_read_frames"]) == summary.frames
    assert (info["width"], info["height"]) == (source["width"], source["height"]) == (1280, 720)
    assert info["avg_frame_rate"] == source["avg_frame_rate"] == "25/1"
    assert not list(run_dir.glob(".*tmp")) and not list(run_dir.glob("*.tmp"))


def test_the_density_grids_are_exported(rendered: tuple[Path, HeatmapSummary]) -> None:
    run_dir, summary = rendered
    with np.load(run_dir / DENSITY_FILE) as npz:
        density = npz["density"]
        assert density.shape == (summary.frames, 36, 64) and density.dtype == np.float32
        assert float(npz["fps"]) == 25.0 and float(npz["sigma"]) == SIGMA
        assert float(npz["scale"]) == summary.scale > 0
        assert float(npz["window_s"]) == 0.5
    assert summary.peak == pytest.approx(float(density.max()))


def test_the_heat_sits_on_the_dot_in_every_frame(rendered: tuple[Path, HeatmapSummary]) -> None:
    run_dir, summary = rendered
    cx, cy = grid_axes(CONFIG)
    with np.load(run_dir / DENSITY_FILE) as npz:
        density = npz["density"]
    errors = []
    for k in range(0, summary.frames, 5):
        j, i = np.unravel_index(int(np.argmax(density[k])), density[k].shape)
        # the heat is the window around the frame, so it must lie on the dot's path in that window
        s = np.linspace(max(k / 25 - 0.5, 0), min(k / 25 + 0.5, DOT.duration_s), 60)
        x, y = DOT.at(s)
        errors.append(float(np.min(np.hypot(cx[i] - x, cy[j] - y))))
    assert np.median(errors) < 0.03 and max(errors) < 0.06


def test_the_drawn_video_is_hot_at_the_dot_and_plain_elsewhere(
    rendered: tuple[Path, HeatmapSummary],
) -> None:
    run_dir, _ = rendered
    k = round(PAUSE_S * 25)
    heat, plain = frame_at(run_dir / HEATMAP_VIDEO_FILE, k), frame_at(VIDEO, k)
    difference = np.abs(heat.astype(np.int16) - plain.astype(np.int16)).mean(axis=2)
    h, w = difference.shape
    yy, xx = np.mgrid[0:h, 0:w]
    near = np.hypot((xx - 320) / w, (yy - 180) / h) < 0.05
    far = np.hypot((xx - 960) / w, (yy - 540) / h) < 0.15
    assert difference[near].mean() > 20
    # Far from the heat only the lossy H.264 encoding differs from the source; how much depends on
    # the ffmpeg build (2.7 on the CI runner), so the limit is far below the heat's >20.
    assert difference[far].mean() < ENCODING_NOISE


def test_frames_without_gaze_are_the_stimulus_frame(tmp_path: Path) -> None:
    render_heatmap(VIDEO, tmp_path, [], SIGMA, CONFIG)
    heat, plain = frame_at(tmp_path / HEATMAP_VIDEO_FILE, 100), frame_at(VIDEO, 100)
    assert np.abs(heat.astype(np.int16) - plain.astype(np.int16)).mean() < ENCODING_NOISE
    with np.load(tmp_path / DENSITY_FILE) as npz:
        assert npz["density"].sum() == 0 and float(npz["scale"]) == 1.0


def test_colour_scale_is_a_percentile_of_the_non_zero_densities() -> None:
    d = np.zeros((2, 4, 4), dtype=np.float32)
    d[0, 0, :] = np.arange(1, 5)
    d[1, 1, :] = np.arange(5, 9)  # 8 positive values 1..8
    assert colour_scale(d, 100.0) == 8.0
    assert colour_scale(d, 50.0) == pytest.approx(4.5)
    assert colour_scale(np.zeros((1, 2, 2), dtype=np.float32), 99.0) == 1.0


def test_overlay_is_transparent_where_cold_and_follows_the_alpha_limit() -> None:
    frame = np.full((72, 128, 3), 100, dtype=np.uint8)
    grid = np.zeros((36, 64), dtype=np.float32)
    assert np.array_equal(overlay(frame, grid, 1.0, CONFIG), frame)
    grid[:] = 5.0  # above the scale: fully hot everywhere
    hot = overlay(frame, grid, 1.0, CONFIG).astype(np.float64)
    colour = cv2.applyColorMap(np.full((1, 1), 255, np.uint8), cv2.COLORMAP_TURBO)[0, 0]
    expected = 100 * (1 - CONFIG.heatmap_alpha_max) + colour * CONFIG.heatmap_alpha_max
    assert np.abs(hot[36, 64] - expected).max() <= 1.0


def test_dots_are_drawn_only_when_asked_and_at_the_participants_median() -> None:
    config = AnalysisConfig(heatmap_dots=True)
    frame = np.zeros((72, 128, 3), dtype=np.uint8)
    p = StimSamples(np.array([1.0, 1.1, 1.2]), np.array([0.5, 0.5, 0.5]), np.array([0.25] * 3))
    draw_dots(frame, [p, StimSamples(np.zeros(0), np.zeros(0), np.zeros(0))], 1.1, config)
    white = np.argwhere((frame == 255).all(axis=2))
    assert len(white) > 0
    assert abs(white[:, 1].mean() - 64) < 1 and abs(white[:, 0].mean() - 18) < 1
    untouched = np.zeros_like(frame)
    draw_dots(untouched, [p], 9.0, config)  # no samples near 9 s
    assert not untouched.any()


def test_a_container_that_reports_too_many_frames_gives_the_frames_it_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from worker.pipeline.replay import FrameReader

    original = FrameReader.__init__

    def inflated(self: FrameReader, path: Path) -> None:
        original(self, path)
        self.frame_count += 12  # the container says 12 frames more than there are

    monkeypatch.setattr(FrameReader, "__init__", inflated)
    summary = render_heatmap(VIDEO, tmp_path, PARTICIPANTS, SIGMA, CONFIG)
    assert summary.frames == 545
    assert int(probe(tmp_path / HEATMAP_VIDEO_FILE)["nb_read_frames"]) == 545
    with np.load(tmp_path / DENSITY_FILE) as npz:
        assert npz["density"].shape[0] == 545


def test_the_dots_option_draws_them_into_the_video(tmp_path: Path) -> None:
    plain_dir, dots_dir = tmp_path / "plain", tmp_path / "dots"
    plain_dir.mkdir()
    dots_dir.mkdir()
    render_heatmap(VIDEO, plain_dir, PARTICIPANTS, SIGMA, CONFIG)
    render_heatmap(VIDEO, dots_dir, PARTICIPANTS, SIGMA, AnalysisConfig(heatmap_dots=True))
    k = round(PAUSE_S * 25)
    plain = frame_at(plain_dir / HEATMAP_VIDEO_FILE, k)
    dots = frame_at(dots_dir / HEATMAP_VIDEO_FILE, k)

    def whites(frame: np.ndarray) -> int:
        return int((frame.min(axis=2) > 235).sum())  # the dot is white; the video is compressed

    assert whites(dots) > whites(plain) + 100
