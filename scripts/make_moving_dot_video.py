"""Generate the moving-dot stimulus video and its ground-truth path (PLAN §12.4).

A dark dot with a light ring moves along a known path on a mid-grey background, with pauses at
every waypoint. The path JSON is computed by the same function that places the dot in each frame,
so the two cannot disagree. Output is deterministic (no timestamps, bit-exact flags).

Run from the repo root or anywhere, with a Python that has numpy and opencv (the backend venv):

    cd backend && uv run python ../scripts/make_moving_dot_video.py

Needs `ffmpeg` on PATH. Writes e2e/fixtures/moving_dot.mp4 and e2e/fixtures/moving_dot_path.json.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

VIDEO_NAME = "moving_dot.mp4"
PATH_NAME = "moving_dot_path.json"

WIDTH = 1280
HEIGHT = 720
FPS = 25
CRF = 20
GOP_FRAMES = FPS  # one keyframe per second, so seeking is cheap

BACKGROUND = (128, 128, 128)  # BGR, neutral mid-grey
DOT_COLOUR = (24, 24, 24)
RING_COLOUR = (235, 235, 235)
DOT_RADIUS_PX = 14
RING_WIDTH_PX = 3
SUBPIXEL_SHIFT = 4  # cv2 draws circles at 1/16 px precision with this shift

INITIAL_PAUSE_S = 2.0
WAYPOINT_PAUSE_S = 1.5
MOVE_S = 0.7

# Waypoints as fractions of (width, height). Start and end at the centre. The first four are the
# inner corners (25 % / 75 %); then the outer positions, in an order that keeps each move short.
CENTRE = (0.5, 0.5)
WAYPOINTS: tuple[tuple[float, float], ...] = (
    (0.25, 0.25),
    (0.75, 0.25),
    (0.75, 0.75),
    (0.25, 0.75),
    (0.10, 0.90),
    (0.10, 0.10),
    (0.90, 0.10),
    (0.90, 0.90),
    CENTRE,
)


@dataclass(frozen=True)
class Pause:
    start_s: float
    end_s: float
    x: float
    y: float


def _px(fraction: tuple[float, float]) -> tuple[float, float]:
    return fraction[0] * WIDTH, fraction[1] * HEIGHT


def _smoothstep(u: float) -> float:
    """Ease in and out: 0 at u = 0, 1 at u = 1, zero speed at both ends."""
    return u * u * (3.0 - 2.0 * u)


def build_pauses() -> list[Pause]:
    """The pause at every waypoint; between two pauses the dot moves for MOVE_S."""
    pauses: list[Pause] = []
    t = 0.0
    for i, point in enumerate((CENTRE, *WAYPOINTS)):
        x, y = _px(point)
        length = INITIAL_PAUSE_S if i == 0 else WAYPOINT_PAUSE_S
        pauses.append(Pause(t, t + length, x, y))
        t += length + MOVE_S
    return pauses


def total_duration_s() -> float:
    """End of the last pause, rounded to whole frames."""
    last = build_pauses()[-1]
    return round(last.end_s * FPS) / FPS


def position_at(t: float, pauses: list[Pause]) -> tuple[float, float]:
    """Dot centre in pixels at time `t` seconds: still inside a pause, eased between two."""
    for previous, following in zip(pauses, pauses[1:], strict=False):
        if previous.start_s <= t <= previous.end_s:
            return previous.x, previous.y
        if previous.end_s < t < following.start_s:
            u = _smoothstep((t - previous.end_s) / (following.start_s - previous.end_s))
            return (
                previous.x + (following.x - previous.x) * u,
                previous.y + (following.y - previous.y) * u,
            )
    last = pauses[-1]
    return last.x, last.y


def render_frame(x: float, y: float) -> np.ndarray:
    """One BGR frame with the dot at (x, y), anti-aliased and sub-pixel positioned."""
    frame = np.full((HEIGHT, WIDTH, 3), BACKGROUND, dtype=np.uint8)
    centre = (round(x * (1 << SUBPIXEL_SHIFT)), round(y * (1 << SUBPIXEL_SHIFT)))
    ring_radius = DOT_RADIUS_PX + RING_WIDTH_PX
    cv2.circle(
        frame,
        centre,
        ring_radius << SUBPIXEL_SHIFT,
        RING_COLOUR,
        -1,
        cv2.LINE_AA,
        SUBPIXEL_SHIFT,
    )
    cv2.circle(
        frame,
        centre,
        DOT_RADIUS_PX << SUBPIXEL_SHIFT,
        DOT_COLOUR,
        -1,
        cv2.LINE_AA,
        SUBPIXEL_SHIFT,
    )
    return frame


def encode_command(output: Path) -> list[str]:
    """ffmpeg reading raw BGR frames from stdin: H.264, constant frame rate, yuv420p, no audio."""
    return [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-",
        "-an",
        "-c:v", "libx264", "-preset", "slow", "-crf", str(CRF), "-g", str(GOP_FRAMES),
        "-pix_fmt", "yuv420p", "-r", str(FPS), "-fps_mode", "cfr",
        "-fflags", "+bitexact", "-flags:v", "+bitexact", "-map_metadata", "-1",
        "-movflags", "+faststart",
        str(output),
    ]  # fmt: skip


def generate(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    pauses = build_pauses()
    frame_count = round(total_duration_s() * FPS)
    samples: list[dict[str, float | int]] = []

    encoder = subprocess.Popen(encode_command(out_dir / VIDEO_NAME), stdin=subprocess.PIPE)
    assert encoder.stdin is not None
    try:
        for n in range(frame_count):
            t = n / FPS
            x, y = position_at(t, pauses)
            samples.append({"t": round(t, 6), "x": round(x, 3), "y": round(y, 3), "frame": n})
            encoder.stdin.write(render_frame(x, y).tobytes())
    finally:
        encoder.stdin.close()
        returncode = encoder.wait()
    if returncode != 0:
        raise RuntimeError(f"ffmpeg failed with exit code {returncode}")

    path = {
        "video": VIDEO_NAME,
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
        "duration_s": frame_count / FPS,
        "dot_radius_px": DOT_RADIUS_PX,
        "samples": samples,
        "pauses": [
            {
                "start_s": p.start_s,
                "end_s": p.end_s,
                "x": round(p.x, 3),
                "y": round(p.y, 3),
            }
            for p in pauses
        ],
    }
    (out_dir / PATH_NAME).write_text(json.dumps(path, indent=1) + "\n")


def main() -> None:
    default_out = Path(__file__).resolve().parent.parent / "e2e" / "fixtures"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out-dir", type=Path, default=default_out)
    generate(parser.parse_args().out_dir)


if __name__ == "__main__":
    main()
