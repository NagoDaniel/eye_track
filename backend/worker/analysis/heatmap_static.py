"""Static heatmaps, one PNG per time segment (PLAN §9.2).

A segment comes from `aois.json` (`segments`) or, without any, fixed windows of
`config.segment_default_s` seconds over the whole video. The density is the one of §9.1 (equal
weight per participant, the run's sigma) over all samples of the segment, drawn over the video frame
at the segment's middle. Each picture has its own colour scale (its own percentile), since a long
segment spreads the same weight over more places than a short one.
"""

import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from app.aois import Segment
from app.storage import atomic_write_bytes
from worker.analysis.config import AnalysisConfig
from worker.analysis.density import StimSamples, group_density
from worker.analysis.heatmap_video import colour_scale, overlay
from worker.pipeline.replay import FrameReader

log = logging.getLogger("worker")

STATIC_DIR = "static"
DEFAULT_ID = "window_{n}"


@dataclass(frozen=True)
class StaticSummary:
    files: list[str]  # relative to the run directory, in segment order
    segments: list[dict[str, float | int | str]]

    def as_dict(self) -> dict[str, object]:
        return {"files": self.files, "segments": self.segments}


def segments_for(
    configured: Sequence[Segment], duration_s: float, config: AnalysisConfig
) -> list[Segment]:
    """The segments of the video, or consecutive windows covering it (the last one may be shorter).
    A segment that starts after the end of the video is dropped, one that ends after it is cut."""
    if configured:
        chosen = [s for s in configured if s.start < duration_s]
        return [Segment(id=s.id, start=s.start, end=min(s.end, duration_s)) for s in chosen]
    count = max(1, math.ceil(duration_s / config.segment_default_s))
    return [
        Segment(
            id=DEFAULT_ID.format(n=n + 1),
            start=n * config.segment_default_s,
            end=min((n + 1) * config.segment_default_s, duration_s),
        )
        for n in range(count)
    ]


def file_name(index: int, segment_id: str) -> str:
    """A file name that is safe whatever the researcher typed as the id."""
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", segment_id).strip("_")[:40] or "segment"
    return f"{STATIC_DIR}/{index + 1:03d}_{slug}.png"


def render_static(
    video_file: Path,
    run_dir: Path,
    participants: Sequence[StimSamples],
    sigma: float,
    segments: Sequence[Segment],
    config: AnalysisConfig,
) -> StaticSummary:
    """Write one PNG per segment into `run_dir/static/`. Raises when the video cannot be read."""
    reader = FrameReader(video_file)
    files: list[str] = []
    described: list[dict[str, float | int | str]] = []
    try:
        for index, segment in enumerate(segments):
            middle = (segment.start + segment.end) / 2
            frame = reader.at(min(round(middle * reader.fps), max(reader.frame_count - 1, 0)))
            if frame is None:
                raise ValueError(f"the stimulus video has no frame at {middle:.2f} s")
            windows = [p.window(segment.start, segment.end) for p in participants]
            grid = group_density(windows, sigma, config).astype(np.float32)
            scale = colour_scale(grid[None], config.heatmap_scale_percentile)
            ok, encoded = cv2.imencode(".png", overlay(frame, grid, scale, config))
            if not ok:
                raise ValueError(f"the picture of segment {segment.id} could not be encoded")
            name = file_name(index, segment.id)
            atomic_write_bytes(run_dir / name, encoded.tobytes())
            files.append(name)
            described.append({
                "id": segment.id, "start": segment.start, "end": segment.end, "file": name,
                "participants_with_samples": sum(1 for x, _ in windows if len(x) > 0),
                "samples": sum(len(x) for x, _ in windows),
            })  # fmt: skip
    finally:
        reader.release()
    return StaticSummary(files, described)
