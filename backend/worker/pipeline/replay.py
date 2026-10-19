"""The debug replay video (PLAN §12.2): what the pipeline made of one session, as a film.

For every stretch of the session in which a stimulus video was on screen it shows that video with
- the raw gaze (orange), the smoothed gaze (blue) and the drift-corrected gaze (green) as dots, and
  the smoothed gaze of the last third of a second as a trail;
- a ring around the centre of the fixation the gaze is in, growing with its duration;
- the participant's webcam in the bottom right corner with the landmarks drawn on it (left out when
  the recording or its landmarks are not there);
- a timeline bar below: orange where the video is paused or stalled, red where no face was found,
  grey where the gaze is invalid, a purple tick at every fullscreen exit, a white cursor.

THE FILE CONTAINS THE PARTICIPANT'S WEBCAM VIDEO. It is only rendered on request and only served to
a logged-in admin; it says so on every frame. Frames are drawn at `replay_fps` along the client
clock, so a pause shows as a frozen video, like the participant saw it.
"""

import logging
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import pyarrow.parquet as pq
from numpy.typing import NDArray

from app.events import parse_events, parse_records
from app.pipeline_names import REPLAY_FILE
from app.schemas import StimulusFrameRecord
from app.storage import derived_dir, events_path, stimulus_frames_path, video_path, webcam_path
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURES_FILE, SUBSET_MESH_INDICES
from worker.pipeline.fixations import runs
from worker.pipeline.gaze import FIXATIONS_FILE, GAZE_FILE
from worker.pipeline.ingest import WEBCAM_FRAMES_FILE
from worker.pipeline.landmarks import LANDMARK_COUNT, LANDMARKS_FILE
from worker.pipeline.stimulus_map import StimulusMap, VideoRectTimeline
from worker.pipeline.sync import SYNC_FILE, read_t_of_pts

log = logging.getLogger("pipeline")

Floats = NDArray[np.float64]
Bools = NDArray[np.bool_]
Frame = NDArray[np.uint8]
Rect = tuple[int, int, int, int]  # x, y, width, height of the video inside the picture
Colour = tuple[int, int, int]

BAR_HEIGHT = 28
NEAREST_ROW_MS = 100.0  # a gaze row further than this from a frame's time is not shown
TRAIL_FRAMES = 10
WEBCAM_INSET_SHARE = 0.25  # of the video width
IRIS_FIRST_MESH_INDEX = 468
SEEK_AHEAD = 90  # reading further ahead than this many frames seeks instead
TEXT_PANEL_WIDTH, TEXT_PANEL_HEIGHT = 450, 92  # the darkened corner behind the text

# Colours are BGR (OpenCV).
ORANGE: Colour = (0, 160, 255)
BLUE: Colour = (230, 120, 0)
GREEN: Colour = (80, 200, 80)
RED: Colour = (60, 60, 230)
GREY: Colour = (140, 140, 140)
PURPLE: Colour = (200, 90, 200)
WHITE: Colour = (255, 255, 255)
DARK: Colour = (30, 30, 30)
CYAN: Colour = (255, 220, 0)
YELLOW: Colour = (0, 230, 255)
BANNER: Colour = (90, 90, 255)  # the warning line: a brighter red than the bar's

# The state of a frame in the timeline bar; the higher wins.
FINE, INVALID_GAZE, NO_FACE, STALLED = 0, 1, 2, 3
BAR_COLOURS: dict[int, Colour] = {
    FINE: (70, 70, 70),
    INVALID_GAZE: GREY,
    NO_FACE: RED,
    STALLED: ORANGE,
}


class ReplayError(Exception):
    """The replay cannot be rendered; the message says why."""


@dataclass(frozen=True)
class ReplaySummary:
    frames: int
    seconds: float
    fps: float
    width: int
    height: int
    videos: list[str]
    webcam_inset: bool
    bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "frames": self.frames,
            "seconds": self.seconds,
            "fps": self.fps,
            "width": self.width,
            "height": self.height,
            "videos": self.videos,
            "webcam_inset": self.webcam_inset,
            "bytes": self.bytes,
        }


# --- Reading videos ---


class FrameReader:
    """Frames of a video file by index, mostly in order (going back or far ahead seeks)."""

    def __init__(self, path: Path) -> None:
        self._capture = cv2.VideoCapture(str(path))
        if not self._capture.isOpened():
            raise ReplayError(f"{path.name} cannot be opened as a video")
        self.fps = float(self._capture.get(cv2.CAP_PROP_FPS)) or 25.0
        self.frame_count = int(self._capture.get(cv2.CAP_PROP_FRAME_COUNT))
        self._position = 0  # the index of the frame the next `read` returns
        self._last: Frame | None = None
        self._last_index = -1

    def at(self, index: int) -> Frame | None:
        index = max(index, 0)
        if index == self._last_index:
            return self._last
        if index < self._position or index - self._position > SEEK_AHEAD:
            self._capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            self._position = index
        while self._position <= index:
            ok, frame = self._capture.read()
            if not ok:
                return self._last  # past the end: keep showing the last frame
            self._last, self._last_index = np.asarray(frame, dtype=np.uint8), self._position
            self._position += 1
        return self._last

    def release(self) -> None:
        self._capture.release()


class Webcam:
    """The recording with the landmark subset of every frame."""

    def __init__(self, reader: FrameReader, pts: Floats, landmarks: NDArray[np.float32]) -> None:
        self.reader, self.pts, self.landmarks = reader, pts, landmarks

    @staticmethod
    def open(root: Path, study_id: int, session_id: uuid.UUID) -> "Webcam | None":
        """None when the recording, its frame times or its landmarks are not there."""
        derived = derived_dir(root, study_id, session_id)
        video = webcam_path(root, study_id, session_id)
        needed = (video, derived / WEBCAM_FRAMES_FILE, derived / LANDMARKS_FILE)
        if not all(p.is_file() for p in needed):
            return None
        table = pq.read_table(derived / LANDMARKS_FILE, columns=["face", "lm"])
        face = np.asarray(table["face"].to_pylist(), dtype=bool)
        flat = np.zeros((len(face), LANDMARK_COUNT * 3), dtype=np.float32)
        rows = np.flatnonzero(face)
        if len(rows):
            values = table.take(rows)["lm"].combine_chunks().values.to_numpy(zero_copy_only=False)
            flat[rows] = values.reshape(len(rows), -1)
        subset = flat.reshape(len(face), LANDMARK_COUNT, 3)[:, SUBSET_MESH_INDICES, :2].copy()
        subset[~face] = np.nan
        pts = pq.read_table(derived / WEBCAM_FRAMES_FILE)["pts"].to_numpy()
        return Webcam(FrameReader(video), pts, subset)

    def picture(self, pts_s: float, width: int) -> Frame | None:
        """The frame at `pts_s`, `width` wide, with the landmarks drawn on it."""
        index = min(max(int(np.searchsorted(self.pts, pts_s)), 0), len(self.pts) - 1)
        frame = self.reader.at(index)
        if frame is None:
            return None
        height = round(frame.shape[0] * width / frame.shape[1])
        small = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
        for (x, y), mesh in zip(
            self.landmarks[min(index, len(self.landmarks) - 1)], SUBSET_MESH_INDICES, strict=True
        ):
            if np.isfinite(x):
                iris = mesh >= IRIS_FIRST_MESH_INDEX
                cv2.circle(
                    small,
                    (round(x * width), round(y * height)),
                    2 if iris else 1,
                    CYAN if iris else YELLOW,
                    -1,
                )
        return np.asarray(small, dtype=np.uint8)


# --- What happens when ---


@dataclass
class Timeline:
    """Everything the film shows, per output frame."""

    times: Floats  # client time, ms
    which: NDArray[np.int64]  # index into `intervals`
    intervals: list[tuple[str, float, float]]  # (video id, first, last client time)
    stim: Floats  # video time in s; NaN while paused or stalled
    raw: tuple[Floats, Floats]  # gaze in video-normalised coordinates
    smooth: tuple[Floats, Floats]
    corrected: tuple[Floats, Floats]
    valid: Bools
    interpolated: Bools
    face: Bools
    fixations: Floats  # rows (start_t, end_t, duration_ms, cx_vid, cy_vid)
    state: NDArray[np.int8]
    ticks: list[int]  # frames at which fullscreen was left
    boundaries: list[int]  # frames at which another video starts


def _nearest(times: Floats, t: float) -> int:
    k = int(np.searchsorted(times, t))
    if k > 0 and (k == len(times) or abs(times[k - 1] - t) <= abs(times[k] - t)):
        k -= 1
    return k


def build_timeline(
    root: Path, study_id: int, session_id: uuid.UUID, config: PipelineConfig
) -> Timeline:
    derived = derived_dir(root, study_id, session_id)
    if not (derived / GAZE_FILE).is_file():
        raise ReplayError(
            "there is no gaze for this session: process it first "
            "(and the calibration must have succeeded)"
        )
    gaze = pq.read_table(derived / GAZE_FILE)
    events = parse_events(events_path(root, study_id, session_id).read_bytes())
    frames_file = stimulus_frames_path(root, study_id, session_id)
    stimulus = (
        parse_records(frames_file.read_bytes(), StimulusFrameRecord, "stimulus_frames")
        if frames_file.is_file()
        else []
    )

    def floats(table: Any, name: str) -> Floats:
        return np.array(
            [np.nan if v is None else v for v in table[name].to_pylist()], dtype=np.float64
        )

    t = floats(gaze, "t")
    video_ids = np.asarray(gaze["video_id"].to_pylist(), dtype=object)
    on_screen = np.array([v is not None for v in video_ids])
    if not on_screen.any():
        raise ReplayError("the session has no playback: there is no video to replay")

    intervals = [
        (str(video_ids[rows[0]]), float(t[rows[0]]), float(t[rows[-1]]))
        for rows in runs(np.arange(len(t)), on_screen, video_ids)
    ]
    step = 1000.0 / config.replay_fps
    # (clipped: `arange` may overshoot the end by a rounding error, which is off screen)
    grids = [np.minimum(np.arange(first, last + 1e-6, step), last) for _, first, last in intervals]
    times = np.concatenate(grids)
    which = np.concatenate([np.full(len(g), k, dtype=np.int64) for k, g in enumerate(grids)])

    lookup = StimulusMap(stimulus, events, config).lookup(times)
    rows = np.array([_nearest(t, x) for x in times])
    fresh = np.abs(t[rows] - times) <= NEAREST_ROW_MS
    raw = VideoRectTimeline(events).normalise(
        times, floats(gaze, "x_px")[rows], floats(gaze, "y_px")[rows]
    )
    smooth = (floats(gaze, "x_vid")[rows], floats(gaze, "y_vid")[rows])
    corrected = (floats(gaze, "x_vid_dc")[rows], floats(gaze, "y_vid_dc")[rows])
    valid = np.asarray(gaze["valid"].to_pylist(), dtype=bool)[rows] & fresh
    interpolated = np.asarray(gaze["interpolated"].to_pylist(), dtype=bool)[rows]

    fixations = np.empty((0, 5))
    if (derived / FIXATIONS_FILE).is_file():
        table = pq.read_table(derived / FIXATIONS_FILE)
        if table.num_rows:
            names = ("start_t", "end_t", "duration_ms", "cx_vid", "cy_vid")
            fixations = np.column_stack([floats(table, c) for c in names])

    features = pq.read_table(derived / FEATURES_FILE)
    a_ms, b = read_t_of_pts(derived / SYNC_FILE)
    feature_t = a_ms + 1000.0 * b * features["pts"].to_numpy()
    face_rows = [_nearest(feature_t, x) for x in times]
    face = np.asarray(features["face"].to_pylist(), dtype=bool)[face_rows]

    state = np.full(len(times), FINE, dtype=np.int8)
    state[~valid & ~interpolated] = INVALID_GAZE
    state[~face] = NO_FACE
    state[np.isnan(lookup.stim_time)] = STALLED
    leaves = [e.t for e in events if e.type == "fullscreen_change" and not e.data.get("fullscreen")]
    ticks = [
        int(np.argmin(np.abs(times - x)))
        for x in leaves
        if times[0] - step <= x <= times[-1] + step
    ]
    boundaries = [int(np.argmax(which == k)) for k in range(1, len(intervals))]
    return Timeline(times, which, intervals, lookup.stim_time, raw, smooth, corrected, valid,
                    interpolated, face, fixations, state, ticks, boundaries)  # fmt: skip


# --- Drawing ---


def letterbox(frame: Frame, width: int, height: int) -> tuple[Frame, Rect]:
    """The frame scaled to fit `width` x `height`, centred on black, and where it sits."""
    scale = min(width / frame.shape[1], height / frame.shape[0])
    w, h = round(frame.shape[1] * scale), round(frame.shape[0] * scale)
    canvas = np.zeros((height, width, 3), dtype=np.uint8)
    x, y = (width - w) // 2, (height - h) // 2
    canvas[y : y + h, x : x + w] = cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)
    return canvas, (x, y, w, h)


def to_pixels(point: tuple[float, float], rect: Rect) -> tuple[int, int]:
    x, y, w, h = rect
    return round(x + np.clip(point[0], 0, 1) * (w - 1)), round(
        y + np.clip(point[1], 0, 1) * (h - 1)
    )


def marker(
    image: Frame,
    point: tuple[float, float],
    rect: Rect,
    colour: Colour,
    radius: int,
    ring: bool = False,
) -> None:
    """A dot (or ring) at a position given as fractions of the video; off the video it is a cross
    on the border."""
    if not (np.isfinite(point[0]) and np.isfinite(point[1])):
        return
    centre = to_pixels(point, rect)
    if not (0 <= point[0] <= 1 and 0 <= point[1] <= 1):
        cv2.drawMarker(image, centre, colour, cv2.MARKER_CROSS, radius * 3, 2)
    elif ring:
        cv2.circle(image, centre, radius, colour, 2, cv2.LINE_AA)
    else:
        cv2.circle(image, centre, radius, colour, -1, cv2.LINE_AA)
        cv2.circle(image, centre, radius, WHITE, 1, cv2.LINE_AA)


def text(
    image: Frame, message: str, origin: tuple[int, int], colour: Colour, scale: float = 0.5
) -> None:
    cv2.putText(image, message, origin, cv2.FONT_HERSHEY_SIMPLEX, scale, DARK, 3, cv2.LINE_AA)
    cv2.putText(image, message, origin, cv2.FONT_HERSHEY_SIMPLEX, scale, colour, 1, cv2.LINE_AA)


def timeline_bar(width: int, tl: Timeline) -> Frame:
    """The bar under the picture, without the cursor."""
    n = len(tl.state)
    bar = np.full((BAR_HEIGHT, width, 3), 40, dtype=np.uint8)
    for x in range(width):
        bar[2:16, x] = BAR_COLOURS[int(tl.state[min(int(x / width * n), n - 1)])]
    for frame in tl.boundaries:
        bar[:, min(int(frame / n * width), width - 1)] = WHITE
    for frame in tl.ticks:
        x = min(int(frame / n * width), width - 1)
        bar[2:BAR_HEIGHT, max(x - 1, 0) : x + 2] = PURPLE
    return bar


def draw_frame(
    k: int, tl: Timeline, picture: Frame, size: tuple[int, int], bar: Frame,
    trail: list[tuple[float, float]], inset: Frame | None,
) -> Frame:  # fmt: skip
    """One film frame: the video, the gaze, the fixation, the webcam, the text, the bar."""
    width, height = size
    canvas, rect = letterbox(picture, width, height)
    image = np.zeros((height + BAR_HEIGHT, width, 3), dtype=np.uint8)
    image[:height] = canvas

    here = np.flatnonzero((tl.fixations[:, 0] <= tl.times[k]) & (tl.times[k] <= tl.fixations[:, 1]))
    for f in here[:1]:
        marker(
            image,
            (tl.fixations[f, 3], tl.fixations[f, 4]),
            rect,
            GREEN,
            int(18 + min(tl.fixations[f, 2] / 20, 60)),
            ring=True,
        )
    if len(trail) > 1:
        points = np.array([to_pixels(p, rect) for p in trail], dtype=np.int32)
        cv2.polylines(image, [points], False, BLUE, 1, cv2.LINE_AA)
    marker(image, (tl.raw[0][k], tl.raw[1][k]), rect, ORANGE, 4)
    marker(image, (tl.corrected[0][k], tl.corrected[1][k]), rect, GREEN, 5)
    marker(image, (tl.smooth[0][k], tl.smooth[1][k]), rect, BLUE, 6)

    if inset is not None:
        h, w = inset.shape[:2]
        x0, y0 = width - w - 8, height - h - 8
        image[y0 : y0 + h, x0 : x0 + w] = inset
        cv2.rectangle(image, (x0, y0), (x0 + w, y0 + h), WHITE, 1)

    panel = image[:TEXT_PANEL_HEIGHT, :TEXT_PANEL_WIDTH]
    panel[:] = (panel * 0.35).astype(np.uint8)  # darken what is under the text so it can be read
    video_id = tl.intervals[int(tl.which[k])][0]
    stim = f"{tl.stim[k]:.2f} s" if np.isfinite(tl.stim[k]) else "paused / stalled"
    text(image, "DEBUG REPLAY - contains participant video (admins only)", (8, 18), BANNER)
    text(
        image,
        f"video {video_id}   t {(tl.times[k] - tl.times[0]) / 1000:.1f} s   video time {stim}",
        (8, 38),
        WHITE,
    )
    text(
        image,
        "orange: raw   blue: smoothed   green: drift-corrected, fixation",
        (8, 58),
        WHITE,
        0.45,
    )
    if not tl.valid[k] and not tl.interpolated[k]:
        text(image, "gaze invalid" + ("" if tl.face[k] else " (no face)"), (8, 78), GREY)

    cursor = min(int(k / len(tl.times) * width), width - 1)
    with_cursor = bar.copy()
    with_cursor[:, max(cursor - 1, 0) : cursor + 2] = WHITE
    image[height:] = with_cursor
    return image


def film(
    tl: Timeline,
    readers: dict[str, FrameReader],
    webcam: Webcam | None,
    a_ms: float,
    b: float,
    size: tuple[int, int],
) -> Iterator[Frame]:
    """The frames in order; a paused or stalled video keeps showing its last picture."""
    width, height = size
    bar = timeline_bar(width, tl)
    picture = np.zeros((height, width, 3), dtype=np.uint8)
    trail: list[tuple[float, float]] = []
    for k in range(len(tl.times)):
        reader = readers[tl.intervals[int(tl.which[k])][0]]
        if np.isfinite(tl.stim[k]):
            frame = reader.at(round(float(tl.stim[k]) * reader.fps))
            picture = frame if frame is not None else picture
        point = (float(tl.smooth[0][k]), float(tl.smooth[1][k]))
        trail = ([*trail, point] if np.isfinite(point[0]) else [])[-TRAIL_FRAMES:]
        inset = None
        if webcam is not None:
            inset = webcam.picture(
                (tl.times[k] - a_ms) / (1000.0 * b), int(width * WEBCAM_INSET_SHARE)
            )
        yield draw_frame(k, tl, picture, size, bar, trail, inset)


# --- Encoding and the whole render ---


def encode(target: Path, size: tuple[int, int], fps: float, frames: Iterator[Frame]) -> None:
    """Pipe BGR frames into ffmpeg (H.264, yuv420p, faststart), writing to `target`."""
    width, height = size
    command = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{width}x{height}", "-r", str(fps), "-i", "-",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", "-f", "mp4", str(target),
    ]  # fmt: skip
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin is not None and process.stderr is not None
    try:
        for image in frames:
            process.stdin.write(np.ascontiguousarray(image).tobytes())
        process.stdin.close()
        error = process.stderr.read().decode(errors="replace")
        if process.wait() != 0:
            raise ReplayError(f"ffmpeg could not encode the replay: {error[-600:].strip()}")
    except BaseException:
        process.kill()
        raise


def render_replay(
    root: Path, study_id: int, session_id: uuid.UUID, config: PipelineConfig
) -> ReplaySummary:
    """Render `derived/debug/replay.mp4` (through a temporary file). Raises `ReplayError`."""
    tl = build_timeline(root, study_id, session_id, config)
    derived = derived_dir(root, study_id, session_id)
    a_ms, b = read_t_of_pts(derived / SYNC_FILE)

    readers: dict[str, FrameReader] = {}
    webcam: Webcam | None = None
    target = derived / "debug" / REPLAY_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        for video_id, _, _ in tl.intervals:
            path = video_path(root, int(video_id))
            if not path.is_file():
                raise ReplayError(
                    f"the stimulus video {video_id} is not on this machine ({path}); "
                    "upload it again"
                )
            readers.setdefault(video_id, FrameReader(path))
        first = readers[tl.intervals[0][0]].at(0)
        if first is None:
            raise ReplayError("the first stimulus video has no frames")
        width = config.replay_width - config.replay_width % 2
        height = round(first.shape[0] * width / first.shape[1])
        height -= height % 2
        webcam = Webcam.open(root, study_id, session_id)
        log.info("rendering the debug replay", extra={"frames": len(tl.times)})
        encode(temporary, (width, height + BAR_HEIGHT + BAR_HEIGHT % 2), config.replay_fps,
               film(tl, readers, webcam, a_ms, b, (width, height)))  # fmt: skip
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
        for reader in readers.values():
            reader.release()
        if webcam is not None:
            webcam.reader.release()
    n = len(tl.times)
    return ReplaySummary(
        frames=n, seconds=n / config.replay_fps, fps=config.replay_fps, width=width,
        height=height + BAR_HEIGHT, videos=[v for v, _, _ in tl.intervals],
        webcam_inset=webcam is not None, bytes=target.stat().st_size,
    )  # fmt: skip
