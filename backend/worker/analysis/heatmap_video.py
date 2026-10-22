"""The dynamic heatmap video (PLAN §9.1).

For each stimulus frame at media time `s`, the group density of the samples with `stim_time` in
`[s - window, s + window]` is drawn over the decoded frame (colour map with transparency) and
written as H.264 MP4 at the stimulus frame rate. The per-frame density grids go to a `.npz`. One
colour scale serves the whole video, so frames can be compared (DECISIONS 2026-10-19 item 5).
"""

import logging
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from numpy.typing import NDArray

from app.storage import atomic_writer
from worker.analysis.config import AnalysisConfig
from worker.analysis.density import StimSamples, group_density
from worker.cancel import check_cancelled
from worker.pipeline.replay import FrameReader, encode

log = logging.getLogger("worker")

HEATMAP_VIDEO_FILE = "heatmap.mp4"
DENSITY_FILE = "density.npz"
DOT_RADIUS_SHARE = 0.008  # of the frame height
Floats = NDArray[np.float64]
Image = NDArray[np.uint8]


@dataclass(frozen=True)
class HeatmapSummary:
    frames: int
    fps: float
    width: int
    height: int
    scale: float  # the density that is drawn fully hot
    peak: float

    def as_dict(self) -> dict[str, float | int]:
        return {"frames": self.frames, "fps": self.fps, "width": self.width,
                "height": self.height, "scale": self.scale, "peak": self.peak}  # fmt: skip


def frame_densities(
    participants: Sequence[StimSamples],
    fps: float,
    frames: int,
    sigma: float,
    config: AnalysisConfig,
) -> NDArray[np.float32]:
    """The group density of every stimulus frame, shape `(frames, grid_height, grid_width)`."""
    out = np.zeros((frames, config.grid_height, config.grid_width), dtype=np.float32)
    for k in range(frames):
        if k % 50 == 0:
            check_cancelled()
        s = k / fps
        window = [p.window(s - config.heatmap_window_s, s + config.heatmap_window_s)
                  for p in participants]  # fmt: skip
        out[k] = group_density(window, sigma, config)
    return out


def colour_scale(densities: NDArray[np.float32], percentile: float) -> float:
    """The given percentile of the non-zero densities of all frames; 1 when there are none."""
    positive = densities[densities > 0]
    return float(np.percentile(positive, percentile)) if positive.size else 1.0


def overlay(frame: Image, grid: NDArray[np.float32], scale: float, config: AnalysisConfig) -> Image:
    """The frame with the density grid on it: hotter is more opaque and warmer."""
    height, width = frame.shape[:2]
    heat = np.clip(grid / scale, 0.0, 1.0).astype(np.float32)
    heat = cv2.resize(heat, (width, height), interpolation=cv2.INTER_CUBIC)
    heat = np.clip(heat, 0.0, 1.0)
    colour = cv2.applyColorMap((heat * 255).astype(np.uint8), cv2.COLORMAP_TURBO)
    alpha = (heat * config.heatmap_alpha_max)[:, :, None]
    mixed = frame.astype(np.float32) * (1 - alpha) + colour.astype(np.float32) * alpha
    return np.asarray(mixed.round().astype(np.uint8))


def draw_dots(
    frame: Image, participants: Sequence[StimSamples], s: float, config: AnalysisConfig
) -> None:
    """One white dot per participant with samples around `s` (in place)."""
    height, width = frame.shape[:2]
    radius = max(2, round(DOT_RADIUS_SHARE * height))
    for p in participants:
        x, y = p.window(s - config.heatmap_window_s, s + config.heatmap_window_s)
        if len(x) == 0:
            continue
        centre = (round(float(np.median(x)) * width), round(float(np.median(y)) * height))
        cv2.circle(frame, centre, radius + 1, (0, 0, 0), -1, cv2.LINE_AA)
        cv2.circle(frame, centre, radius, (255, 255, 255), -1, cv2.LINE_AA)


def _frames(
    reader: FrameReader,
    densities: NDArray[np.float32],
    participants: Sequence[StimSamples],
    scale: float,
    size: tuple[int, int],
    config: AnalysisConfig,
    written: list[int],
) -> Iterator[Image]:
    """The drawn frames. The frame count a container reports can be too high: the frames stop at the
    last one that decodes, and `written[0]` says how many there were."""
    width, height = size
    for k in range(len(densities)):
        if k % 50 == 0:
            check_cancelled()
        picture = reader.at(k)
        if picture is None or reader.last_index < k:
            return
        written[0] = k + 1
        picture = np.ascontiguousarray(picture[:height, :width])
        out = overlay(picture, densities[k], scale, config)
        if config.heatmap_dots:
            draw_dots(out, participants, k / reader.fps, config)
        yield out


def render_heatmap(
    video_file: Path,
    run_dir: Path,
    participants: Sequence[StimSamples],
    sigma: float,
    config: AnalysisConfig,
) -> HeatmapSummary:
    """Write `heatmap.mp4` and `density.npz` into the run directory. Raises when the video cannot
    be read or encoded."""
    reader = FrameReader(video_file)
    try:
        frames, fps = reader.frame_count, reader.fps
        if frames <= 0:
            raise ValueError(f"{video_file.name} has no frames")
        first = reader.at(0)
        if first is None:
            raise ValueError(f"{video_file.name} has no readable first frame")
        # H.264 with yuv420p needs even sides.
        size = (first.shape[1] - first.shape[1] % 2, first.shape[0] - first.shape[0] % 2)

        log.info("computing the heatmap densities", extra={"frames": frames})
        densities = frame_densities(participants, fps, frames, sigma, config)
        scale = colour_scale(densities, config.heatmap_scale_percentile)

        target = run_dir / HEATMAP_VIDEO_FILE
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        written = [0]
        try:
            drawn = _frames(reader, densities, participants, scale, size, config, written)
            encode(temporary, size, fps, drawn)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        reader.release()

    if written[0] == 0:
        raise ValueError(f"{video_file.name} could not be decoded")
    if written[0] < frames:
        log.warning("the video has fewer frames than its container says",
                    extra={"declared": frames, "decoded": written[0]})  # fmt: skip
        frames, densities = written[0], densities[: written[0]]

    with atomic_writer(run_dir / DENSITY_FILE) as f:
        np.savez_compressed(
            f, density=densities, fps=fps, sigma=sigma, scale=scale,
            window_s=config.heatmap_window_s,
        )  # fmt: skip
    return HeatmapSummary(frames, fps, size[0], size[1], scale, float(densities.max()))
