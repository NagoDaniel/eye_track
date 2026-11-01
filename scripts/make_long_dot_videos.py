"""Generate the two long moving-dot videos for the T13 recordings, and their ground-truth paths.

Each part (about 2 minutes) has, after a centre start:
- 40 rests of 1.5 s: the 20 points of a 5 x 4 grid (10 % to 90 % of the width and height), twice,
  each pass in its own fixed shuffled order (different in each part), joined by 0.7 s moves;
- smooth pursuit: a figure-eight through the centre (10 s), then one turn around an ellipse (8 s);
- a final rest at the centre.

Played as a two-video playlist, the study's validation block "between videos" falls in the middle
of the 4 minutes. Drawing and encoding are those of `make_moving_dot_video.py` (same dot, same
codec settings, deterministic output). The path JSON has the same fields as `moving_dot_path.json`
plus `pursuits` (the pursuit segments). Run with the backend venv:

    cd backend && uv run python ../scripts/make_long_dot_videos.py

Writes backend/lab/videos/long_dot_{1,2}.mp4 and long_dot_{1,2}_path.json.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_moving_dot_video as base

FPS, WIDTH, HEIGHT = base.FPS, base.WIDTH, base.HEIGHT
REST_S = 1.5
MOVE_S = 0.7
INITIAL_REST_S = 2.0
FINAL_REST_S = 1.5
PURSUIT_START_REST_S = 0.5
GRID_X = (0.10, 0.30, 0.50, 0.70, 0.90)
GRID_Y = (0.10, 0.37, 0.63, 0.90)
PASSES = 2
FIGURE_EIGHT_S = 10.0
FIGURE_EIGHT_AMPLITUDE = (0.35, 0.30)  # fractions of width and height
ELLIPSE_S = 8.0
ELLIPSE_RADII = (0.35, 0.35)
SEEDS = {1: 11, 2: 22}  # the shuffle of each part

Point = tuple[float, float]  # fractions of width and height


@dataclass(frozen=True)
class Segment:
    """`position(u)` for u from 0 to 1 over `duration_s`; kind rest, move or pursuit."""

    kind: str
    duration_s: float
    position: Callable[[float], Point]
    name: str = ""


def rest(p: Point, duration_s: float = REST_S) -> Segment:
    return Segment("rest", duration_s, lambda _u: p)


def move(a: Point, b: Point) -> Segment:
    def at(u: float) -> Point:
        s = base._smoothstep(u)
        return a[0] + (b[0] - a[0]) * s, a[1] + (b[1] - a[1]) * s

    return Segment("move", MOVE_S, at)


def figure_eight() -> Segment:
    ax, ay = FIGURE_EIGHT_AMPLITUDE
    return Segment(
        "pursuit", FIGURE_EIGHT_S,
        lambda u: (0.5 + ax * math.sin(2 * math.pi * u), 0.5 + ay * math.sin(4 * math.pi * u)),
        "figure_eight",
    )  # fmt: skip


def ellipse(clockwise: bool) -> Segment:
    rx, ry = ELLIPSE_RADII
    sign = 1.0 if clockwise else -1.0  # y grows downwards: +sin turns clockwise on screen

    def at(u: float) -> Point:
        a = 2 * math.pi * u
        return 0.5 + rx * math.cos(a), 0.5 + sign * ry * math.sin(a)

    return Segment("pursuit", ELLIPSE_S, at, "ellipse")


def design(part: int) -> list[Segment]:
    rng = random.Random(SEEDS[part])
    grid = [(x, y) for y in GRID_Y for x in GRID_X]
    order: list[Point] = []
    for _ in range(PASSES):
        points = grid[:]
        rng.shuffle(points)
        order += points
    centre = (0.5, 0.5)
    segments = [rest(centre, INITIAL_REST_S)]
    here = centre
    for p in order:
        segments += [move(here, p), rest(p)]
        here = p
    start = (0.5 + ELLIPSE_RADII[0], 0.5)
    segments += [
        move(here, centre), rest(centre), figure_eight(),
        move(centre, start), rest(start, PURSUIT_START_REST_S), ellipse(clockwise=part == 1),
        move(start, centre), rest(centre, FINAL_REST_S),
    ]  # fmt: skip
    return segments


def timeline(segments: list[Segment]) -> list[tuple[float, float, Segment]]:
    out, t = [], 0.0
    for s in segments:
        out.append((t, t + s.duration_s, s))
        t += s.duration_s
    return out


def position_at(t: float, spans: list[tuple[float, float, Segment]]) -> Point:
    for start, end, s in spans:
        if t <= end:
            return s.position(min(max((t - start) / s.duration_s, 0.0), 1.0))
    return spans[-1][2].position(1.0)


def generate(part: int, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    spans = timeline(design(part))
    duration = round(spans[-1][1] * FPS) / FPS
    frames = round(duration * FPS)
    video = out_dir / f"long_dot_{part}.mp4"
    samples: list[dict[str, float | int]] = []
    encoder = subprocess.Popen(base.encode_command(video), stdin=subprocess.PIPE)
    assert encoder.stdin is not None
    try:
        for n in range(frames):
            t = n / FPS
            fx, fy = position_at(t, spans)
            x, y = fx * WIDTH, fy * HEIGHT
            samples.append({"t": round(t, 6), "x": round(x, 3), "y": round(y, 3), "frame": n})
            encoder.stdin.write(base.render_frame(x, y).tobytes())
    finally:
        encoder.stdin.close()
        code = encoder.wait()
    if code != 0:
        raise RuntimeError(f"ffmpeg failed with exit code {code}")

    def px(p: Point) -> dict[str, float]:
        return {"x": round(p[0] * WIDTH, 3), "y": round(p[1] * HEIGHT, 3)}

    path = {
        "video": video.name,
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
        "duration_s": frames / FPS,
        "dot_radius_px": base.DOT_RADIUS_PX,
        "samples": samples,
        "pauses": [
            {"start_s": round(a, 6), "end_s": round(b, 6), **px(s.position(0.0))}
            for a, b, s in spans
            if s.kind == "rest"
        ],  # fmt: skip
        "pursuits": [
            {"start_s": round(a, 6), "end_s": round(b, 6), "kind": s.name}
            for a, b, s in spans
            if s.kind == "pursuit"
        ],  # fmt: skip
    }
    out = out_dir / f"long_dot_{part}_path.json"
    out.write_text(json.dumps(path, indent=1) + "\n")
    return video


def main() -> None:
    default = Path(__file__).resolve().parent.parent / "backend" / "lab" / "videos"
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out-dir", type=Path, default=default)
    out = parser.parse_args().out_dir
    for part in (1, 2):
        print(generate(part, out))


if __name__ == "__main__":
    main()
