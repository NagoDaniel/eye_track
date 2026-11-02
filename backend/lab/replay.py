"""Replay films for the lab: a recorded dot video with the gaze of several experiments on top.

For one session and one dot video the lab re-runs each chosen experiment on that session alone
(`replay_work/<experiment>/data`, the product's stages unchanged), then draws one film with
- the dot video the participant watched (the true dot is part of it),
- one dot per experiment, in its own colour: its drift-corrected, smoothed gaze (`x_vid_dc`),
- the participant's webcam with the landmarks in the corner (`worker.pipeline.replay.Webcam`),
- a legend with each experiment's median error to the dot while the dot is paused (`dc_pause`),
- the timeline bar of the product's replay (orange stalled, red no face, grey invalid gaze).

The drawing helpers and the encoder are the product's (`worker/pipeline/replay.py`). THE FILM
CONTAINS THE PARTICIPANT'S WEBCAM: it stays under `lab_data/` (not committed) and says so.
"""

import hashlib
import shutil
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import numpy as np
import pyarrow.parquet as pq
from numpy.typing import NDArray

from app.storage import derived_dir, session_dir
from lab.dots import KNOWN, DotVideo, read_dot_videos
from lab.experiment import Experiment
from lab.layout import SessionRef, list_sessions, sessions_root
from lab.run import Failure, prepare_session, run_stages, score_session
from worker.pipeline.config import PipelineConfig
from worker.pipeline.gaze import GAZE_FILE
from worker.pipeline.replay import (
    BANNER,
    BAR_HEIGHT,
    WEBCAM_INSET_SHARE,
    WHITE,
    Colour,
    Frame,
    FrameReader,
    ReplayError,
    Timeline,
    Webcam,
    build_timeline,
    encode,
    letterbox,
    marker,
    text,
    timeline_bar,
)
from worker.pipeline.sync import SYNC_FILE, read_t_of_pts

Floats = NDArray[np.float64]

REPLAYS_DIR = "replays"
WORK_DIR = "replay_work"
DOT_RADIUS = 7
LEGEND_ROW_PX = 20
LEGEND_WIDTH_PX = 470
# Colours are BGR (OpenCV); one per experiment in the order given.
TRACK_COLOURS: tuple[Colour, ...] = (
    (0, 160, 255), (230, 120, 0), (80, 200, 80), (200, 90, 200),
    (255, 220, 0), (0, 230, 255), (120, 120, 255), (200, 200, 200),
)  # fmt: skip
MAX_EXPERIMENTS = len(TRACK_COLOURS)
Progress = Callable[[str, float], None]


@dataclass(frozen=True)
class Track:
    """One experiment's gaze for the film."""

    label: str
    colour: Colour
    x: Floats  # video-normalised coordinates, per film frame
    y: Floats
    dc_pause: float | None  # median error to the dot while it rests, viewport px


def find_session(lab_data: Path, study_id: int, code: str) -> SessionRef:
    refs = [r for r in list_sessions(sessions_root(lab_data), study_id) if r.code == code]
    if len(refs) != 1:
        raise ReplayError(f"study {study_id} has {len(refs)} sessions with code {code} in the lab")
    return refs[0]


def dot_videos(lab_data: Path, ref: SessionRef) -> list[DotVideo]:
    return read_dot_videos(session_dir(sessions_root(lab_data), ref.study_id, ref.session_id))


def run_on_session(
    lab_data: Path, ref: SessionRef, exp: Experiment, dots: Sequence[DotVideo]
) -> tuple[Path, Failure | None]:
    """Run `exp` on this one session in its own work root; returns the root."""
    work = lab_data / WORK_DIR / exp.name / "data"
    if work.parent.exists():
        shutil.rmtree(work.parent)
    work.mkdir(parents=True)
    prepare_session(sessions_root(lab_data), work, ref)
    _, failures = run_stages(work, ref, exp.config, ("a",), exp.variant, dots)
    return work, (failures[0] if failures else None)


def _nearest_rows(t: Floats, times: Floats) -> NDArray[np.int64]:
    k = np.clip(np.searchsorted(t, times), 1, len(t) - 1)
    return np.where(np.abs(t[k - 1] - times) <= np.abs(t[k] - times), k - 1, k).astype(np.int64)


def _floats(table: object, name: str) -> Floats:
    values = table[name].to_pylist()  # type: ignore[index]
    return np.array([np.nan if v is None else v for v in values], dtype=np.float64)


def _track(
    work: Path, ref: SessionRef, times: Floats, label: str, colour: Colour, dc_pause: float | None
) -> Track:
    gaze = pq.read_table(derived_dir(work, ref.study_id, ref.session_id) / GAZE_FILE)
    rows = _nearest_rows(_floats(gaze, "t"), times)
    return Track(label, colour, _floats(gaze, "x_vid_dc")[rows], _floats(gaze, "y_vid_dc")[rows],
                 dc_pause)  # fmt: skip


def _rows_of(tl: Timeline, video_ids: set[str]) -> NDArray[np.int64]:
    """The film frames in which one of `video_ids` was on screen."""
    wanted = [k for k, (vid, _, _) in enumerate(tl.intervals) if vid in video_ids]
    keep = np.flatnonzero(np.isin(tl.which, wanted)).astype(np.int64)
    if len(keep) == 0:
        raise ReplayError("the session did not play that video")
    return keep


def _only_video(tl: Timeline, keep: NDArray[np.int64]) -> Timeline:
    def pair(a: tuple[Floats, Floats]) -> tuple[Floats, Floats]:
        return a[0][keep], a[1][keep]

    return replace(
        tl, times=tl.times[keep], which=tl.which[keep], stim=tl.stim[keep], raw=pair(tl.raw),
        smooth=pair(tl.smooth), corrected=pair(tl.corrected), valid=tl.valid[keep],
        interpolated=tl.interpolated[keep], face=tl.face[keep], state=tl.state[keep],
        ticks=[], boundaries=[], pipeline_b=None,
    )  # fmt: skip


def _frame(
    k: int, tl: Timeline, tracks: Sequence[Track], keep: NDArray[np.int64], picture: Frame,
    size: tuple[int, int], bar: Frame, inset: Frame | None, title: str,
) -> Frame:  # fmt: skip
    width, height = size
    canvas, rect = letterbox(picture, width, height)
    image = np.zeros((height + BAR_HEIGHT, width, 3), dtype=np.uint8)
    image[:height] = canvas
    for track in tracks:
        marker(image, (track.x[keep[k]], track.y[keep[k]]), rect, track.colour, DOT_RADIUS)
    if inset is not None:
        h, w = inset.shape[:2]
        x0, y0 = width - w - 8, height - h - 8
        image[y0 : y0 + h, x0 : x0 + w] = inset
        cv2.rectangle(image, (x0, y0), (x0 + w, y0 + h), WHITE, 1)
    rows = 2 + len(tracks)
    panel = image[: rows * LEGEND_ROW_PX + 6, :LEGEND_WIDTH_PX]
    panel[:] = (panel * 0.35).astype(np.uint8)
    text(image, "LAB REPLAY - contains participant video, do not share", (8, 16), BANNER, 0.45)
    stim = f"{tl.stim[k]:.1f} s" if np.isfinite(tl.stim[k]) else "paused / stalled"
    text(image, f"{title}   video time {stim}", (8, 16 + LEGEND_ROW_PX), WHITE, 0.45)
    for i, track in enumerate(tracks):
        y = 16 + (2 + i) * LEGEND_ROW_PX
        cv2.circle(image, (14, y - 4), 5, track.colour, -1, cv2.LINE_AA)
        error = "" if track.dc_pause is None else f"   {track.dc_pause:.0f} px"
        text(image, f"{track.label}{error}", (26, y), WHITE, 0.45)
    if not tl.valid[k] and not tl.interpolated[k]:
        text(image, "gaze invalid" + ("" if tl.face[k] else " (no face)"),
             (8, 16 + (2 + len(tracks)) * LEGEND_ROW_PX), (140, 140, 140))  # fmt: skip
    cursor = min(int(k / len(tl.times) * width), width - 1)
    with_cursor = bar.copy()
    with_cursor[:, max(cursor - 1, 0) : cursor + 2] = WHITE
    image[height:] = with_cursor
    return image


def render_replay(
    lab_data: Path,
    study_id: int,
    code: str,
    video: str,
    experiments: Sequence[Experiment],
    progress: Progress | None = None,
) -> tuple[Path, list[Failure]]:
    """Run `experiments` on the session and write the film; returns it and the experiments that
    could not be run (left out of the film)."""
    say = progress or (lambda _message, _share: None)
    if not experiments:
        raise ReplayError("choose at least one experiment")
    if len(experiments) > MAX_EXPERIMENTS:
        raise ReplayError(f"at most {MAX_EXPERIMENTS} experiments in one film")
    ref = find_session(lab_data, study_id, code)
    dots = dot_videos(lab_data, ref)
    wanted = [d for d in dots if d.name == video]
    if not wanted:
        raise ReplayError(
            f"the session did not play '{video}' (it played {[d.name for d in dots]})"
        )

    done: list[tuple[Experiment, Path]] = []
    failures: list[Failure] = []
    for i, exp in enumerate(experiments):
        say(f"running {exp.name}", i / (len(experiments) + 1))
        work, failure = run_on_session(lab_data, ref, exp, dots)
        if failure is not None:
            failures.append(failure)
        else:
            done.append((exp, work))
    if not done:
        raise ReplayError("no experiment could be run: " + "; ".join(f.reason for f in failures))

    first_exp, first_work = done[0]
    full = build_timeline(first_work, study_id, ref.session_id, first_exp.config)
    keep = _rows_of(full, {d.video_id for d in wanted})
    tl = _only_video(full, keep)
    tracks = []
    for n, (exp, work) in enumerate(done):
        scores = [
            s
            for s in score_session(
                work, derived_dir(work, study_id, ref.session_id), ref, "a", wanted, exp.config
            )  # fmt: skip
        ]
        dc_pause = scores[0].dc_pause if scores else None
        tracks.append(_track(work, ref, full.times, exp.name, TRACK_COLOURS[n], dc_pause))

    derived = derived_dir(first_work, study_id, ref.session_id)
    a_ms, b = read_t_of_pts(derived / SYNC_FILE)
    reader = FrameReader(KNOWN[video][0])
    webcam = Webcam.open(first_work, study_id, ref.session_id)
    first = reader.at(0)
    if first is None:
        raise ReplayError(f"{video} has no frames")
    config: PipelineConfig = first_exp.config
    width = config.replay_width - config.replay_width % 2
    height = round(first.shape[0] * width / first.shape[1])
    height -= height % 2
    bar = timeline_bar(width, tl)
    title = f"{code}  {video}"

    def frames() -> Iterator[Frame]:
        picture = np.zeros((height, width, 3), dtype=np.uint8)
        for k in range(len(tl.times)):
            if k % 30 == 0:
                say("drawing the film", (len(done) + k / len(tl.times)) / (len(done) + 1))
            if np.isfinite(tl.stim[k]):
                frame = reader.at(round(float(tl.stim[k]) * reader.fps))
                picture = frame if frame is not None else picture
            inset = None
            if webcam is not None:
                inset = webcam.picture((tl.times[k] - a_ms) / (1000.0 * b),
                                       int(width * WEBCAM_INSET_SHARE))  # fmt: skip
            yield _frame(k, tl, tracks, keep, picture, (width, height), bar, inset, title)

    target = lab_data / REPLAYS_DIR / replay_name(study_id, code, video, [e.name for e, _ in done])
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        encode(temporary, (width, height + BAR_HEIGHT + BAR_HEIGHT % 2), config.replay_fps,
               frames())  # fmt: skip
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
        reader.release()
        if webcam is not None:
            webcam.reader.release()
    say("done", 1.0)
    return target, failures


def replay_name(study_id: int, code: str, video: str, experiments: Sequence[str]) -> str:
    joined = "+".join(experiments)
    if len(joined) > 80:
        joined = joined[:60] + "-" + hashlib.sha256(joined.encode()).hexdigest()[:8]
    return f"{study_id}_{code}_{video}__{joined}.mp4"
