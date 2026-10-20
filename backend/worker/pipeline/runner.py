"""The stage runner (PLAN §8): run the stages of `process_session` in order.

Every stage reads files and writes files under `raw/` and `derived/` and can be run again; its
duration and a summary go into `derived/quality.json` (`stages`). A run can start at a stage
(`from_stage`, the later stages follow) or run one stage only; stages before the first one that
runs keep the summary of the last run that included them.

A stage that raises stops the run with `StageFailed` naming it; the earlier stages' files stay.
A calibration that failed is not an error: the gaze stage is skipped, its old files are removed and
the quality stage reports the session as excluded (DECISIONS milestone 3 item 15).
"""

import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.logging import bind_context
from app.stage_status import read_entries, status_of, write_entries
from app.storage import atomic_write_bytes, derived_dir, webcam_path
from worker.pipeline.calibration import run_calibration
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURES_FILE, compute_features
from worker.pipeline.gaze import run_gaze
from worker.pipeline.ingest import ChunkRecord, ingest
from worker.pipeline.landmarks import LANDMARKS_FILE, detect_landmarks
from worker.pipeline.plots import run_plots
from worker.pipeline.quality import QUALITY_FILE, build_quality
from worker.pipeline.spec import SPEC_A, PipelineSpec
from worker.pipeline.sync import SYNC_FILE, run_sync

log = logging.getLogger("pipeline")

# Session flags set by the pipeline (and removed again when a run no longer finds the problem).
CALIBRATION_FAILED_FLAG = "calibration_failed"
SYNC_FLAG_PREFIX = "sync_"


class StageFailed(Exception):
    """A stage raised; `stage` is its name and the cause is chained."""

    def __init__(self, stage: str, cause: BaseException) -> None:
        super().__init__(f"stage '{stage}' failed: {type(cause).__name__}: {cause}")
        self.stage = stage


@dataclass
class Context:
    root: Path
    study_id: int
    session_id: uuid.UUID
    config: PipelineConfig
    model: Path  # the MediaPipe face landmarker
    max_inflated_bytes: int
    manifest: Sequence[ChunkRecord] | None = None  # the chunks table rows; ingest needs them
    keep_intermediates: bool = False  # keep the files a stage normally removes (debugging)
    stages: dict[str, dict[str, Any]] = field(default_factory=dict)  # results so far, for quality

    @property
    def derived(self) -> Path:
        return derived_dir(self.root, self.study_id, self.session_id)


StageFn = Callable[[Context], dict[str, Any]]


def _ingest(ctx: Context) -> dict[str, Any]:
    if ctx.manifest is None:
        raise ValueError("ingest needs the chunk table rows of the session")
    summary = ingest(
        ctx.root, ctx.study_id, ctx.session_id, ctx.manifest, ctx.max_inflated_bytes,
        ctx.keep_intermediates,
    )  # fmt: skip
    return summary.as_dict()


def _landmarks(ctx: Context) -> dict[str, Any]:
    video = webcam_path(ctx.root, ctx.study_id, ctx.session_id)
    return detect_landmarks(ctx.model, video, ctx.derived / LANDMARKS_FILE).as_dict()


def _features(ctx: Context) -> dict[str, Any]:
    return compute_features(
        ctx.derived / LANDMARKS_FILE, ctx.derived / FEATURES_FILE, ctx.config
    ).as_dict()


def _sync(ctx: Context) -> dict[str, Any]:
    r = run_sync(ctx.root, ctx.study_id, ctx.session_id, ctx.config)
    return {
        "lag_ms": r.lag_ms,
        "peak": r.peak,
        "b": r.b,
        "windows": len(r.windows),
        "flags": r.flags,
    }


def calibration_stage(ctx: Context, spec: PipelineSpec = SPEC_A) -> dict[str, Any]:
    """The calibration stage of a pipeline: fit, write its calibration file, summarise."""
    fit = run_calibration(ctx.root, ctx.study_id, ctx.session_id, ctx.config, spec)
    cv = None if fit.selection is None else fit.selection.chosen
    return {
        "status": fit.status,
        "failed_reason": fit.failed_reason,
        "attempt": None if fit.attempt is None else fit.attempt.number,
        "degree": None if cv is None else cv.degree,
        "cv_error_px": None if cv is None else cv.error_px,
        "samples": sum(fit.kept_per_dot),
    }


def _calibration(ctx: Context) -> dict[str, Any]:
    return calibration_stage(ctx, SPEC_A)


def calibration_status(derived: Path, spec: PipelineSpec = SPEC_A) -> str | None:
    path = derived / spec.calibration_file
    if not path.exists():
        return None
    status = json.loads(path.read_text(encoding="utf-8"))["status"]
    return str(status)


def gaze_stage(ctx: Context, spec: PipelineSpec = SPEC_A) -> dict[str, Any]:
    """The gaze stage of a pipeline; skipped, with its old files removed, when its calibration
    failed (DECISIONS milestone 3 item 15)."""
    if calibration_status(ctx.derived, spec) != "ok":
        for name in (spec.gaze_file, spec.fixations_file):  # derived, so stale files can go
            (ctx.derived / name).unlink(missing_ok=True)
        return {"skipped": "calibration_failed"}
    return run_gaze(ctx.root, ctx.study_id, ctx.session_id, ctx.config, spec).as_dict()


def _gaze(ctx: Context) -> dict[str, Any]:
    return gaze_stage(ctx, SPEC_A)


def _quality(ctx: Context) -> dict[str, Any]:
    report = build_quality(ctx.root, ctx.study_id, ctx.session_id, ctx.config, ctx.stages)
    return {"included": report["inclusion"]["included"], "reasons": report["inclusion"]["reasons"]}


def _plots(ctx: Context) -> dict[str, Any]:
    return run_plots(ctx.root, ctx.study_id, ctx.session_id, ctx.config).as_dict()


STAGES: dict[str, StageFn] = {
    "ingest": _ingest,
    "landmarks": _landmarks,
    "features": _features,
    "sync": _sync,
    "calibration": _calibration,
    "gaze": _gaze,
    "quality": _quality,
    "plots": _plots,
}


def _previous_stages(derived: Path) -> dict[str, dict[str, Any]]:
    path = derived / QUALITY_FILE
    if not path.exists():
        return {}
    stages = json.loads(path.read_text(encoding="utf-8")).get("stages", {})
    return dict(stages) if isinstance(stages, dict) else {}


def select_stages(names: Sequence[str], from_stage: str | None, only: str | None) -> list[str]:
    if from_stage is not None and only is not None:
        raise ValueError("give from_stage or only, not both")
    for given in (from_stage, only):
        if given is not None and given not in names:
            raise ValueError(f"unknown stage '{given}' (stages: {', '.join(names)})")
    if only is not None:
        return [only]
    start = names.index(from_stage) if from_stage is not None else 0
    return list(names[start:])


def run_pipeline(
    ctx: Context,
    from_stage: str | None = None,
    only: str | None = None,
    stages: Mapping[str, StageFn] = STAGES,
) -> dict[str, dict[str, Any]]:
    """Run the selected stages in order and return the summary of every stage (earlier ones as
    last recorded). Raises `StageFailed`."""
    selected = select_stages(list(stages), from_stage, only)
    ctx.stages = _previous_stages(ctx.derived)
    # What the stages did, kept in its own file (also when one fails). The stages about to run
    # lose their old entry: "not run" is true until they have run.
    entries = read_entries(ctx.derived, {"stages": ctx.stages})
    for name in selected:
        entries.pop(name, None)
    for name in selected:
        with bind_context(stage=name, session_id=str(ctx.session_id)):
            log.info("stage started")
            started = time.perf_counter()
            try:
                summary = stages[name](ctx)
            except Exception as exc:
                log.exception("stage failed")
                entries[name] = {
                    "status": "failed",
                    "seconds": round(time.perf_counter() - started, 3),
                    "summary": None,
                    "error": f"{type(exc).__name__}: {exc}",
                }
                write_entries(ctx.derived, entries)
                raise StageFailed(name, exc) from exc
            seconds = round(time.perf_counter() - started, 3)
            ctx.stages[name] = {"seconds": seconds, "summary": summary}
            entries[name] = {"status": status_of(summary), "seconds": seconds,
                             "summary": summary, "error": None}  # fmt: skip
            write_entries(ctx.derived, entries)
            log.info("stage done", extra={"seconds": seconds})
    quality_path = ctx.derived / QUALITY_FILE
    if quality_path.exists() and ("quality" in selected or "plots" in selected):
        # The quality stage cannot know its own duration (nor the plots after it): put the stage
        # results into the file it wrote.
        report = json.loads(quality_path.read_text(encoding="utf-8"))
        report["stages"] = ctx.stages
        atomic_write_bytes(quality_path, (json.dumps(report, indent=1) + "\n").encode())
    return ctx.stages


def pipeline_flags(derived: Path) -> list[str]:
    """The session flags the stages' files call for: the sync flags and a failed calibration."""
    flags: list[str] = []
    sync = derived / SYNC_FILE
    if sync.exists():
        flags += [str(f) for f in json.loads(sync.read_text(encoding="utf-8")).get("flags", [])]
    if calibration_status(derived) == "failed":
        flags.append(CALIBRATION_FAILED_FLAG)
    return flags


def is_pipeline_flag(flag: str) -> bool:
    return flag == CALIBRATION_FAILED_FLAG or flag.startswith(SYNC_FLAG_PREFIX)
