"""The moving-dot fixture agrees with its ground-truth path JSON (PLAN §12.4).

Reads the committed files in `e2e/fixtures`; never regenerates the video (that is
`scripts/make_moving_dot_video.py`). Uses the real `ffprobe` and decodes frames with OpenCV.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pytest

FIXTURES = Path(__file__).resolve().parents[2] / "e2e" / "fixtures"
VIDEO = FIXTURES / "moving_dot.mp4"
PATH_JSON = FIXTURES / "moving_dot_path.json"

CENTROID_TOLERANCE_PX = 2.0
DARK_THRESHOLD = 60  # grey level below which a pixel belongs to the dark dot (background is 128)
DURATION_TOLERANCE_S = 0.05


@pytest.fixture(scope="module")
def path() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(PATH_JSON.read_text())
    return data


@pytest.fixture(scope="module")
def probe() -> dict[str, Any]:
    out = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries",
            "stream=codec_name,pix_fmt,width,height,r_frame_rate,avg_frame_rate,nb_read_frames"
            ":format=duration",
            "-of", "json", str(VIDEO),
        ],
        check=True, capture_output=True, text=True, timeout=60,
    ).stdout  # fmt: skip
    data: dict[str, Any] = json.loads(out)
    return data


def _dark_centroid(frame: np.ndarray) -> tuple[float, float]:
    grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    ys, xs = np.nonzero(grey < DARK_THRESHOLD)
    assert xs.size > 100, "no dark dot found in the frame"
    return float(xs.mean()), float(ys.mean())


def _read_frames(indices: list[int]) -> dict[int, np.ndarray]:
    """Decode the video sequentially (exact frame numbers) and keep the wanted frames."""
    wanted = set(indices)
    capture = cv2.VideoCapture(str(VIDEO))
    assert capture.isOpened()
    frames: dict[int, np.ndarray] = {}
    n = 0
    try:
        while len(frames) < len(wanted):
            ok, frame = capture.read()
            assert ok, f"video ended before frame {max(wanted)}"
            if n in wanted:
                frames[n] = frame
            n += 1
    finally:
        capture.release()
    return frames


def test_video_stream_is_h264_constant_rate_1280x720(
    probe: dict[str, Any], path: dict[str, Any]
) -> None:
    stream = probe["streams"][0]
    assert stream["codec_name"] == "h264"
    assert stream["pix_fmt"] == "yuv420p"
    assert (stream["width"], stream["height"]) == (1280, 720)
    assert stream["r_frame_rate"] == "25/1"
    assert stream["avg_frame_rate"] == "25/1"
    assert (path["width"], path["height"], path["fps"]) == (1280, 720, 25)
    assert abs(float(probe["format"]["duration"]) - path["duration_s"]) < DURATION_TOLERANCE_S
    assert 15 < path["duration_s"] < 30


def test_one_path_sample_per_frame(probe: dict[str, Any], path: dict[str, Any]) -> None:
    samples = path["samples"]
    assert len(samples) == int(probe["streams"][0]["nb_read_frames"])
    assert [s["frame"] for s in samples] == list(range(len(samples)))
    assert all(abs(s["t"] - s["frame"] / path["fps"]) < 1e-5 for s in samples)
    assert len(samples) == round(path["duration_s"] * path["fps"])


def test_pauses_match_the_samples(path: dict[str, Any]) -> None:
    pauses = path["pauses"]
    assert len(pauses) == 10
    assert pauses[0]["start_s"] == 0 and (pauses[0]["x"], pauses[0]["y"]) == (640, 360)
    assert pauses[-1]["end_s"] == pytest.approx(path["duration_s"])
    assert (pauses[-1]["x"], pauses[-1]["y"]) == (640, 360)
    for pause in pauses:
        inside = [s for s in path["samples"] if pause["start_s"] <= s["t"] <= pause["end_s"]]
        assert inside, pause
        assert all(s["x"] == pause["x"] and s["y"] == pause["y"] for s in inside)
    # Between two pauses the dot is moving.
    for before, after in zip(pauses, pauses[1:], strict=False):
        moving = [s for s in path["samples"] if before["end_s"] < s["t"] < after["start_s"]]
        assert moving
        assert any((s["x"], s["y"]) != (before["x"], before["y"]) for s in moving)


def test_dot_centroid_follows_the_path_in_pauses_and_moves(path: dict[str, Any]) -> None:
    samples = path["samples"]
    pauses = path["pauses"]
    in_pause = [
        next(s["frame"] for s in samples if p["start_s"] <= s["t"] <= p["end_s"]) for p in pauses
    ]
    in_move = [
        next(s["frame"] for s in samples if b["end_s"] < s["t"] < a["start_s"])
        for b, a in zip(pauses, pauses[1:], strict=False)
    ]
    # Mid-move frames, where the dot is fastest.
    mid_move = [
        next(s["frame"] for s in samples if s["t"] >= (b["end_s"] + a["start_s"]) / 2 - 1e-9)
        for b, a in zip(pauses, pauses[1:], strict=False)
    ]
    wanted = sorted({*in_pause, *in_move, *mid_move, len(samples) - 1})
    assert len(wanted) >= 20
    frames = _read_frames(wanted)
    for n in wanted:
        x, y = _dark_centroid(frames[n])
        sample = samples[n]
        assert abs(x - sample["x"]) < CENTROID_TOLERANCE_PX, (n, x, sample)
        assert abs(y - sample["y"]) < CENTROID_TOLERANCE_PX, (n, y, sample)
