"""Score a gaze table against the moving dot: the tables of
`docs/evidence/m6-tuning-baseline/dot_eval.py`, as typed functions.

Units (they differ between the tables; the old baseline files did not say so):
- table 1 comes from the product's `moving_dot_report`: errors in **viewport px** (the CSS px of the
  participant's screen; the video's letterboxed rectangle is usually larger than 1280 x 720);
- tables 2 to 4, as in `dot_eval.py`: **video px**, i.e. the video-normalised gaze times the
  video's own size (1280 x 720 for the moving-dot video).
`viewport_per_video_px` converts: viewport px = video px x that factor.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
from numpy.typing import NDArray

from app.schemas import EventRecord
from worker.pipeline.config import PipelineConfig
from worker.pipeline.moving_dot import DotPath, moving_dot_report
from worker.pipeline.stimulus_map import VideoRectTimeline

Floats = NDArray[np.float64]

# A rest position is scored from this long after the dot arrives (the eyes need time to get there).
REST_SETTLE_S = 0.5
# The affine check fits on the first half of the video's time and scores the second half
# (dot_eval.py split the 21.8 s video at 11 s; half of it is 10.9 s).
# Fewest frames for a fit or a rest-position median.
MIN_FRAMES = 4


@dataclass(frozen=True)
class Rest:
    start_s: float
    end_s: float
    x: float  # video px
    y: float


@dataclass(frozen=True)
class DotTruth:
    path: DotPath
    width: float  # the video's size in px
    height: float
    rests: tuple[Rest, ...]
    pursuits: tuple[tuple[float, float], ...] = ()  # (start_s, end_s) of smooth-pursuit segments

    @staticmethod
    def load(file: Path) -> "DotTruth":
        data = json.loads(file.read_text(encoding="utf-8"))
        rests = tuple(
            Rest(float(p["start_s"]), float(p["end_s"]), float(p["x"]), float(p["y"]))
            for p in data["pauses"]
        )
        pursuits = tuple((float(p["start_s"]), float(p["end_s"])) for p in data.get("pursuits", []))
        return DotTruth(DotPath(data), float(data["width"]), float(data["height"]), rests, pursuits)


@dataclass(frozen=True)
class AxisFit:
    slope: float  # gaze = slope * dot + intercept; below 1: pulled towards the centre
    intercept: float  # video px
    r: float


@dataclass(frozen=True)
class Score:
    """One session, pipeline and dot video against the dot."""

    study_id: int
    code: str
    pipeline: str
    valid_fraction: float
    # table 1, viewport px
    median: float
    pause: float
    move: float
    dc_median: float | None
    dc_pause: float | None
    dc_move: float | None
    dc_p90: float | None
    lag_ms: float | None
    within: float  # share of frames within dot_max_median_error_norm of the diagonal
    viewport_per_video_px: float
    # tables 2 to 4, video px
    x: AxisFit | None
    y: AxisFit | None
    rest_error: tuple[tuple[float, float] | None, ...]  # (dx, dy) = gaze - dot, per rest
    affine_before: float | None  # second-half median error as measured ...
    affine_after: float | None  # ... and after a gain/offset fitted on the first half
    video: str = "moving_dot"  # the dot video's name (lab/dots.py)
    pursuit: float | None = None  # median error during smooth pursuit, viewport px (as measured)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Score":
        values = dict(data)
        for axis in ("x", "y"):
            values[axis] = None if data[axis] is None else AxisFit(**data[axis])
        values["rest_error"] = tuple(
            None if r is None else (r[0], r[1]) for r in data["rest_error"]
        )
        return Score(**values)


def _floats(table: pa.Table, name: str) -> Floats:
    return np.array([np.nan if v is None else v for v in table[name].to_pylist()], dtype=np.float64)


@dataclass(frozen=True)
class DotFrames:
    """The valid playback frames inside the dot's path: stimulus time, gaze and dot in video px."""

    s: Floats
    x: Floats
    y: Floats
    dot_x: Floats
    dot_y: Floats


def dot_frames(
    gaze: pa.Table,
    truth: DotTruth,
    video_id: str | None = None,
    x_name: str = "x_vid",
    y_name: str = "y_vid",
) -> DotFrames:
    """`video_id`: only the frames of that video (None: every playback frame)."""
    phase = np.asarray(gaze["phase"].to_pylist(), dtype=object)
    if video_id is not None:
        phase = np.where(np.asarray(gaze["video_id"].to_pylist(), dtype=object) == video_id,
                         phase, "other")  # fmt: skip
    valid = np.asarray(gaze["valid"].to_pylist(), dtype=bool)
    st = _floats(gaze, "stim_time")
    with np.errstate(invalid="ignore"):
        m = (
            (phase == "playback")
            & valid
            & np.isfinite(st)
            & (st >= 0)
            & (st <= truth.path.duration_s)
        )
    x, y, s = _floats(gaze, x_name)[m] * truth.width, _floats(gaze, y_name)[m] * truth.height, st[m]
    ok = np.isfinite(x) & np.isfinite(y)
    x, y, s = x[ok], y[ok], s[ok]
    tx, ty = truth.path.at(s)
    return DotFrames(s, x, y, tx * truth.width, ty * truth.height)


def axis_fit(dot: Floats, gaze: Floats) -> AxisFit | None:
    """None when the dot or the gaze does not vary (no slope or correlation is defined)."""
    if len(dot) < MIN_FRAMES or np.ptp(dot) == 0 or np.ptp(gaze) == 0:
        return None
    slope, intercept = np.polyfit(dot, gaze, 1)
    return AxisFit(float(slope), float(intercept), float(np.corrcoef(dot, gaze)[0, 1]))


def rest_errors(f: DotFrames, truth: DotTruth) -> tuple[tuple[float, float] | None, ...]:
    out: list[tuple[float, float] | None] = []
    for r in truth.rests:
        sel = (f.s >= r.start_s + REST_SETTLE_S) & (f.s <= r.end_s)
        if sel.sum() < MIN_FRAMES:
            out.append(None)
        else:
            out.append((float(np.median(f.x[sel]) - r.x), float(np.median(f.y[sel]) - r.y)))
    return tuple(out)


def affine_check(f: DotFrames, split_s: float) -> tuple[float | None, float | None]:
    """Fit dot = a * gaze + b per axis before `split_s`, score from it on."""
    h1, h2 = f.s < split_s, f.s >= split_s
    if h1.sum() < MIN_FRAMES or h2.sum() < MIN_FRAMES:
        return None, None
    px, py = np.polyfit(f.x[h1], f.dot_x[h1], 1), np.polyfit(f.y[h1], f.dot_y[h1], 1)
    before = np.hypot(f.x - f.dot_x, f.y - f.dot_y)[h2]
    after = np.hypot(np.polyval(px, f.x) - f.dot_x, np.polyval(py, f.y) - f.dot_y)[h2]
    return float(np.median(before)), float(np.median(after))


def _scale(gaze: pa.Table, events: Sequence[EventRecord], truth: DotTruth) -> float:
    """Median width of the video's rectangle on screen over the video's own width, in playback."""
    phase = np.asarray(gaze["phase"].to_pylist(), dtype=object)
    t = _floats(gaze, "t")[phase == "playback"]
    width, _ = VideoRectTimeline(events).rect_at(t)
    finite = width[np.isfinite(width)]
    return float(np.median(finite)) / truth.width if len(finite) else float("nan")


def pursuit_error(f: DotFrames, truth: DotTruth) -> float | None:
    """Median distance (video px) during the smooth-pursuit segments."""
    sel = np.zeros(len(f.s), dtype=np.bool_)
    for start, end in truth.pursuits:
        sel |= (f.s >= start) & (f.s <= end)
    if sel.sum() < MIN_FRAMES:
        return None
    return float(np.median(np.hypot(f.x - f.dot_x, f.y - f.dot_y)[sel]))


def score_gaze(
    gaze: pa.Table,
    events: Sequence[EventRecord],
    truth: DotTruth,
    config: PipelineConfig,
    study_id: int,
    code: str,
    pipeline: str,
    video_id: str | None = None,
    video: str = "moving_dot",
) -> Score:
    report = moving_dot_report(gaze, events, truth.path, config, video_id)
    u, c = report["uncorrected"], report["drift_corrected"]
    f = dot_frames(gaze, truth, video_id)
    before, after = affine_check(f, truth.path.duration_s / 2)
    scale = _scale(gaze, events, truth)
    pursuit = pursuit_error(f, truth)
    return Score(
        study_id=study_id, code=code, pipeline=pipeline,
        valid_fraction=report["valid_fraction"],
        median=u["median_px"], pause=u["pauses"]["median_px"], move=u["movements"]["median_px"],
        dc_median=c["median_px"], dc_pause=c["pauses"]["median_px"],
        dc_move=c["movements"]["median_px"], dc_p90=c["p90_px"],
        lag_ms=report["gaze_lag_ms"], within=u["within_threshold"],
        viewport_per_video_px=scale,
        x=axis_fit(f.dot_x, f.x), y=axis_fit(f.dot_y, f.y),
        rest_error=rest_errors(f, truth),
        affine_before=before, affine_after=after, video=video,
        pursuit=None if pursuit is None else pursuit * scale,
    )  # fmt: skip


# --- The report ---


def _n(value: float | None, width: int, spec: str = ".0f") -> str:
    return " " * (width - 1) + "-" if value is None else f"{value:{width}{spec}}"


SHORT = {"moving_dot": "dot", "long_dot_1": "L1", "long_dot_2": "L2"}


def short_name(name: str) -> str:
    return SHORT.get(name, name)


def format_scores(scores: Sequence[Score], truths: Mapping[str, DotTruth]) -> str:
    """The four tables; `vid` is the dot video (dot = the 22 s video, L1 and L2 the two parts of
    the long recording)."""
    lines = [
        "1. distance to the dot in viewport px (moving_dot_report); scale: viewport px / video px;",
        "   pursuit = median during the smooth-pursuit segments (long videos only)",
        f"{'study':>5} {'code':8}{'vid':4}{'pl':3}{'valid':>6}{'unc med':>8}{'unc pause':>10}"
        f"{'unc move':>9}{'dc med':>7}{'dc pause':>9}{'dc move':>8}{'dc p90':>7}{'lag ms':>7}"
        f"{'<=thr%':>7}{'scale':>6}{'pursuit':>8}",
    ]
    for s in scores:
        lines.append(
            f"{s.study_id:5} {s.code:8}{short_name(s.video):4}{s.pipeline:3}{s.valid_fraction:6.2f}"
            f"{_n(s.median, 8)}{_n(s.pause, 10)}{_n(s.move, 9)}{_n(s.dc_median, 7)}"
            f"{_n(s.dc_pause, 9)}{_n(s.dc_move, 8)}{_n(s.dc_p90, 7)}{_n(s.lag_ms, 7)}"
            f"{s.within * 100:7.0f}{s.viewport_per_video_px:6.2f}{_n(s.pursuit, 8)}"
        )
    lines += ["", "2. gaze = a * dot + b per axis (video px), correlation r"]

    def fit(f: AxisFit | None) -> str:
        if f is None:
            return "      (no fit)          "
        return f"a={f.slope:.2f} b={f.intercept:+5.0f}px r={f.r:.2f}"

    for s in scores:
        lines.append(
            f"{s.study_id:5} {s.code} {short_name(s.video):3} {s.pipeline}  "
            f"x: {fit(s.x)}   y: {fit(s.y)}"
        )
    lines += ["", "3. signed error (gaze - dot) at each rest position, video px, mean +- spread "
              "over the sessions of a study"]  # fmt: skip
    pipelines = sorted({s.pipeline for s in scores})
    for study, video in sorted({(s.study_id, s.video) for s in scores}):
        lines.append(f"study {study}, video {video}")
        mine = [s for s in scores if s.study_id == study and s.video == video]
        for i, r in enumerate(truths[video].rests):
            cells = []
            for p in pipelines:
                errs = [s.rest_error[i] for s in mine if s.pipeline == p]
                known = np.array([e for e in errs if e is not None], dtype=np.float64)
                if len(known) == 0:
                    cells.append(f"{p.upper()} {'-':>27}")
                    continue
                cells.append(
                    f"{p.upper()} dx {known[:, 0].mean():+5.0f}+-{known[:, 0].std():3.0f} "
                    f"dy {known[:, 1].mean():+5.0f}+-{known[:, 1].std():3.0f}"
                )
            lines.append(f"{i:2} ({r.x:5.0f},{r.y:4.0f}) | " + " | ".join(cells))
    lines += ["", "4. gain/offset fitted on the first half of the video's time, median error on "
              "the second half, video px"]  # fmt: skip
    for s in scores:
        lines.append(
            f"{s.study_id:5} {s.code} {short_name(s.video):3} {s.pipeline}  second half: "
            f"{_n(s.affine_before, 4)} px as measured, {_n(s.affine_after, 4)} px after the "
            "affine fit"
        )
    return "\n".join(lines) + "\n"
