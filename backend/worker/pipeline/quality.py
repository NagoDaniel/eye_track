"""Session quality report, `derived/quality.json`, and the inclusion decision (PLAN §8.10, §4.1).

Collects what the stages found: valid fraction during playback, webcam frame rate, face-lost
episodes, fullscreen exits and stalls, the browser gate attempts, the calibration fit, validation
accuracy and precision per block with the drift, the sync result and its flags, and whether the
session is included in the analysis according to the study's `inclusion` thresholds, with the
reasons when it is not. The runner (T11) adds the duration and summary of every stage.
"""

import json
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow.parquet as pq
from numpy.typing import NDArray

from app.events import parse_events
from app.schemas import EventRecord, InclusionConfig, StudyConfig
from app.storage import atomic_write_bytes, derived_dir, events_path, meta_path
from worker.pipeline.calibration import CALIBRATION_FILE
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURES_FILE
from worker.pipeline.gaze import GAZE_FILE, phase_intervals
from worker.pipeline.ingest import WEBCAM_FRAMES_FILE
from worker.pipeline.sync import SYNC_FILE, read_t_of_pts
from worker.pipeline.validation import validate_session

QUALITY_FILE = "quality.json"
QUALITY_VERSION = 1

Floats = NDArray[np.floating[Any]]
Bools = NDArray[np.bool_]


# --- Inclusion ---


@dataclass(frozen=True)
class Inclusion:
    included: bool
    reasons: list[str]
    criteria: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"included": self.included, "reasons": self.reasons, "criteria": self.criteria}


def decide_inclusion(
    *,
    is_debug: bool,
    calibration_ok: bool,
    playback_frames: int,
    valid_fraction: float | None,
    validation_error_norm: float | None,
    inclusion: InclusionConfig,
) -> Inclusion:
    """Is the session used in the analysis? Every reason it is not is listed. Debug sessions are
    left out by default (PLAN §12.1). The two thresholds are the study's (`inclusion`, PLAN §4.1);
    a value equal to its threshold passes."""
    reasons: list[str] = []
    if is_debug:
        reasons.append("debug_session")
    if not calibration_ok:
        reasons.append("calibration_failed")
    else:
        if playback_frames == 0:
            reasons.append("no_playback_data")
        elif valid_fraction is not None and valid_fraction < inclusion.min_valid_fraction:
            reasons.append("valid_fraction_low")
        if validation_error_norm is None:
            reasons.append("no_validation_data")
        elif validation_error_norm > inclusion.max_validation_error_norm:
            reasons.append("validation_error_high")
    return Inclusion(
        included=not reasons,
        reasons=reasons,
        criteria={
            "valid_fraction": valid_fraction,
            "min_valid_fraction": inclusion.min_valid_fraction,
            "validation_error_norm": validation_error_norm,
            "max_validation_error_norm": inclusion.max_validation_error_norm,
        },
    )


# --- Pieces ---


def webcam_stats(pts: Floats, gap_ms: float) -> dict[str, Any]:
    """Frame count, duration and effective frame rate of the recording."""
    if len(pts) < 2:
        return {"frames": len(pts), "duration_s": 0.0, "fps_mean": None, "fps_median": None,
                "gaps": 0, "max_gap_ms": None}  # fmt: skip
    steps = np.diff(pts) * 1000.0
    duration = float(pts[-1] - pts[0])
    return {
        "frames": len(pts),
        "duration_s": duration,
        "fps_mean": (len(pts) - 1) / duration if duration > 0 else None,
        "fps_median": float(1000.0 / np.median(steps)) if np.median(steps) > 0 else None,
        "gaps": int((steps > gap_ms).sum()),
        "max_gap_ms": float(steps.max()),
    }


def face_lost(
    face: Bools, t_ms: Floats, min_ms: float, within: Bools | None = None
) -> dict[str, Any]:
    """Stretches of frames without a face that last at least `min_ms` (a stretch lasts from its
    first frame to the first frame with a face again). `within` restricts the frames considered;
    a stretch ends where the mask does."""
    count, total, longest = 0, 0.0, 0.0
    n = len(face)
    i = 0
    while i < n:
        if face[i] or (within is not None and not within[i]):
            i += 1
            continue
        j = i
        while j + 1 < n and not face[j + 1] and (within is None or within[j + 1]):
            j += 1
        end = t_ms[j + 1] if j + 1 < n else t_ms[j] + (t_ms[j] - t_ms[j - 1] if j > 0 else 0.0)
        length = float(end - t_ms[i])
        if length >= min_ms:
            count += 1
            total += length
            longest = max(longest, length)
        i = j + 1
    return {"episodes": count, "total_ms": total, "longest_ms": longest}


def video_spans(events: Sequence[EventRecord]) -> list[tuple[float, float]]:
    """(load, end) of every video; a video without an end event lasts to the end of the session."""
    spans: list[tuple[float, float]] = []
    for load in (e for e in events if e.type == "video_load"):
        end = next((e.t for e in events if e.type == "video_ended" and e.t >= load.t), float("inf"))
        spans.append((load.t, end))
    return spans


def event_counts(events: Sequence[EventRecord]) -> dict[str, Any]:
    """Fullscreen exits, stalls and the participant leaving the page, during recording."""
    starts = [e.t for e in events if e.type == "recorder_start"]
    stops = [e.t for e in events if e.type == "recorder_stop"]
    lo, hi = (starts[0] if starts else 0.0), (stops[-1] if stops else float("inf"))
    spans = video_spans(events)

    def in_video(t: float) -> bool:
        return any(a <= t <= b for a, b in spans)

    def during(e: EventRecord) -> bool:
        return lo <= e.t <= hi

    exits = [
        e
        for e in events
        if e.type == "fullscreen_change" and not e.data.get("fullscreen") and during(e)
    ]
    stalls = [e for e in events if e.type == "video_stall" and during(e)]
    # A video's first `waiting` comes with its start (media time 0); only later ones are stalls.
    midway = [e for e in stalls if float(e.data.get("media_time", 0)) > 0]
    left = [
        e for e in events
        if during(e) and ((e.type == "visibility_change" and e.data.get("state") == "hidden")
                          or e.type == "window_blur")
    ]  # fmt: skip
    return {
        "fullscreen_exits": len(exits),
        "fullscreen_exits_in_playback": sum(1 for e in exits if in_video(e.t)),
        "stalls": len(midway),
        "stalls_at_start": len(stalls) - len(midway),
        "page_left": len(left),
        "browser_face_lost_episodes": sum(1 for e in events if e.type == "face_lost"),
    }


def gate_results(events: Sequence[EventRecord]) -> list[dict[str, Any]]:
    out = []
    for e in events:
        if e.type == "calib_gate_result":
            d = e.data
            out.append({k: d.get(k) for k in ("block", "attempt", "passed", "skipped",
                                              "mean_error_norm", "reason")})  # fmt: skip
    return out


# --- The report ---


def _frac(a: int, b: int) -> float | None:
    return a / b if b else None


def _mean(values: Sequence[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return float(np.mean(present)) if present else None


def build_quality(
    root: Path,
    study_id: int,
    session_id: uuid.UUID,
    config: PipelineConfig,
    stages: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the report from the session's files and write `derived/quality.json`.

    `stages` (name -> duration and summary, from the runner) is put in as given. With a failed
    calibration there is no gaze, so the playback and validation parts are null and the session is
    excluded. Raises if a file of an earlier stage is missing.
    """
    derived = derived_dir(root, study_id, session_id)
    meta = json.loads(meta_path(root, study_id, session_id).read_text(encoding="utf-8"))
    study_config = StudyConfig.model_validate(meta["config"])
    events = parse_events(events_path(root, study_id, session_id).read_bytes())
    calibration = json.loads((derived / CALIBRATION_FILE).read_text(encoding="utf-8"))
    sync = json.loads((derived / SYNC_FILE).read_text(encoding="utf-8"))
    pts = pq.read_table(derived / WEBCAM_FRAMES_FILE)["pts"].to_numpy()
    features = pq.read_table(derived / FEATURES_FILE)
    a_ms, b = read_t_of_pts(derived / SYNC_FILE)
    t_ms = a_ms + 1000.0 * b * features["pts"].to_numpy()
    face = np.asarray(features["face"].to_pylist(), dtype=np.bool_)
    face_ratio = features["face_ratio"].to_numpy(zero_copy_only=False)

    ok = calibration["status"] == "ok"
    gaze = pq.read_table(derived / GAZE_FILE) if ok else None
    phase = None if gaze is None else np.asarray(gaze["phase"].to_pylist(), dtype=object)
    playback = None if phase is None else phase == "playback"

    # --- playback
    playback_part: dict[str, Any] | None = None
    valid_fraction: float | None = None
    playback_frames = 0
    if gaze is not None and playback is not None:
        valid = np.asarray(gaze["valid"].to_pylist(), dtype=np.bool_)
        interpolated = np.asarray(gaze["interpolated"].to_pylist(), dtype=np.bool_)
        blink = np.asarray(gaze["blink"].to_pylist(), dtype=np.bool_)
        video = np.asarray(gaze["video_id"].to_pylist(), dtype=object)
        playback_frames = int(playback.sum())
        valid_fraction = _frac(int((valid & playback).sum()), playback_frames)
        per_video = {
            str(v): {
                "frames": int(((video == v) & playback).sum()),
                "valid_fraction": _frac(int((valid & playback & (video == v)).sum()),
                                        int(((video == v) & playback).sum())),
            }
            for v in sorted({v for v in video if v is not None})
        }  # fmt: skip
        playback_part = {
            "frames": playback_frames,
            "valid_frames": int((valid & playback).sum()),
            "valid_fraction": valid_fraction,
            "blink_fraction": _frac(int((blink & playback).sum()), playback_frames),
            "interpolated_fraction": _frac(int((interpolated & playback).sum()), playback_frames),
            "per_video": per_video,
        }

    # --- face size relative to the calibration (the study's warn multipliers)
    ratio_part: dict[str, Any] | None = None
    in_calibration = np.zeros(len(t_ms), dtype=np.bool_)
    for start, end, name in phase_intervals(events):
        if name == "calibration":
            in_calibration |= (t_ms >= start) & (t_ms <= end)
    reference_frames = in_calibration & face & np.isfinite(face_ratio)
    if reference_frames.any():
        reference = float(np.median(face_ratio[reference_frames]))
        q = study_config.quality
        outside = None
        if playback is not None:
            seen = playback & face & np.isfinite(face_ratio)
            low = face_ratio < q.min_face_ratio_warn * reference
            high = face_ratio > q.max_face_ratio_warn * reference
            outside = _frac(int((seen & (low | high)).sum()), int(seen.sum()))
        ratio_part = {
            "calibration_median": reference,
            "min_warn": q.min_face_ratio_warn,
            "max_warn": q.max_face_ratio_warn,
            "playback_share_outside": outside,
        }

    # --- validation
    validation_part: dict[str, Any] | None = None
    validation_error: float | None = None
    if ok:
        result = validate_session(root, study_id, session_id, config)
        blocks = result.as_dict()["blocks"]
        validation_error = _mean([blk["mean_error_norm"] for blk in blocks])
        validation_part = {
            **result.as_dict(),
            "mean_error_norm": validation_error,
            "mean_error_corrected_norm": _mean(
                [blk["mean_error_corrected_norm"] for blk in blocks]
            ),
        }

    inclusion = decide_inclusion(
        is_debug=bool(meta.get("is_debug")),
        calibration_ok=ok,
        playback_frames=playback_frames,
        valid_fraction=valid_fraction,
        validation_error_norm=validation_error,
        inclusion=study_config.inclusion,
    )
    report: dict[str, Any] = {
        "version": QUALITY_VERSION,
        "config": config.as_dict(),
        "session_flags": list(meta.get("flags", [])),
        "is_debug": bool(meta.get("is_debug")),
        "webcam": webcam_stats(pts, config.quality_frame_gap_ms),
        "face_lost": {
            "all": face_lost(face, t_ms, config.quality_face_lost_min_ms),
            "playback": None
            if playback is None
            else face_lost(face, t_ms, config.quality_face_lost_min_ms, playback),
        },
        "events": event_counts(events),
        "playback": playback_part,
        "face_ratio": ratio_part,
        "browser_gate": gate_results(events),
        "calibration": {
            "status": calibration["status"],
            "failed_reason": calibration["failed_reason"],
            "attempt_used": calibration.get("attempt_used"),
            "attempts": calibration.get("attempts", []),
            "cv": calibration.get("cv"),
            "samples": calibration.get("samples"),
        },
        "validation": validation_part,
        "sync": {k: sync[k] for k in ("lag_ms", "peak", "t_of_pts", "residual_max_ms", "flags")},
        "inclusion": inclusion.as_dict(),
        "stages": stages or {},
    }
    atomic_write_bytes(derived / QUALITY_FILE, (json.dumps(report, indent=1) + "\n").encode())
    return report
