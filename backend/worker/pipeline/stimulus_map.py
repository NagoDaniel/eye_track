"""Client time to video and stimulus time (PLAN §8.2 step 5).

The `stimulus_frames` stream lists every frame of the stimulus video that the browser presented:
`expected_display_time` (client clock, ms) and `media_time` (s). A client time inside a frame's
interval gets a media time by linear interpolation between that frame and the next. Gaps give no
stimulus time: a pause or a stall is a step in which the wall clock advanced by more than the media
time (a dropped frame advances both and is not a gap), and so is a seek (media time going back).
The `video_*` events say which video the frames belong to (the records carry no video id) and when
it left the screen.
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from app.schemas import EventRecord, StimulusFrameRecord
from worker.pipeline.config import PipelineConfig


@dataclass(frozen=True)
class StimulusLookup:
    video_id: list[str | None]  # the video on screen at each time, else None
    stim_time: NDArray[np.float64]  # media time in seconds; NaN in a gap and outside a video


class _Video:
    def __init__(
        self,
        video_id: str,
        frames: list[StimulusFrameRecord],
        end: float | None,
        tolerance_ms: float,
    ) -> None:
        ordered = sorted(frames, key=lambda f: f.expected_display_time)
        kept = [ordered[0]]
        for f in ordered[1:]:
            if f.expected_display_time > kept[-1].expected_display_time:
                kept.append(f)  # a repeated display time adds nothing
        self.video_id = video_id
        self.e = np.array([f.expected_display_time for f in kept])
        self.m = np.array([f.media_time for f in kept])
        steps = np.diff(self.e)
        self.frame_ms = float(np.median(steps)) if len(steps) else 0.0
        wall, media = steps, np.diff(self.m) * 1000
        self.gap = (wall - media > tolerance_ms) | (media < 0)
        # The video is on screen from its first frame until its end event, or one frame after the
        # last frame when there is none.
        self.start = float(self.e[0])
        self.end = end if end is not None else float(self.e[-1]) + self.frame_ms

    def stim_time(self, t: float) -> float:
        k = int(np.searchsorted(self.e, t, side="right")) - 1
        if k == len(self.e) - 1:  # on the last frame: it stays up for about one frame
            return (
                float(self.m[k] + (t - self.e[k]) / 1000)
                if t - self.e[k] <= self.frame_ms
                else np.nan
            )
        if self.gap[k]:
            return np.nan
        fraction = (t - self.e[k]) / (self.e[k + 1] - self.e[k])
        return float(self.m[k] + (self.m[k + 1] - self.m[k]) * fraction)


class StimulusMap:
    """Maps client times (ms) to the video on screen and its media time."""

    def __init__(
        self,
        frames: Sequence[StimulusFrameRecord],
        events: Sequence[EventRecord],
        config: PipelineConfig,
    ) -> None:
        loads = [e for e in events if e.type == "video_load"]
        if frames and not loads:
            raise ValueError("there are stimulus frames but no video_load event to say which video")
        self._videos: list[_Video] = []
        for i, load in enumerate(loads):
            until = loads[i + 1].t if i + 1 < len(loads) else float("inf")
            video_id = str(load.data["video_id"])
            mine = [f for f in frames if load.t <= f.t < until]
            if not mine:
                continue  # skipped (debug mode) or never presented
            ended = [
                e.t
                for e in events
                if e.type == "video_ended"
                and load.t <= e.t < until
                and str(e.data.get("video_id")) == video_id
            ]
            self._videos.append(
                _Video(video_id, mine, ended[0] if ended else None, config.stim_gap_tolerance_ms)
            )
        first = min((f.t for f in frames), default=None)
        if first is not None and loads and first < loads[0].t:
            raise ValueError("a stimulus frame was presented before the first video_load event")

    def lookup(self, t_ms: NDArray[np.float64] | Sequence[float]) -> StimulusLookup:
        times = np.asarray(t_ms, dtype=np.float64)
        ids: list[str | None] = [None] * len(times)
        stim = np.full(len(times), np.nan)
        for video in self._videos:
            for i in np.flatnonzero((times >= video.start) & (times <= video.end)):
                ids[i] = video.video_id
                stim[i] = video.stim_time(float(times[i]))
        return StimulusLookup(video_id=ids, stim_time=stim)
