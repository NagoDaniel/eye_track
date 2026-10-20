"""The stages of Pipeline B (PLAN §8.5 to §8.10), run by the `process_pipeline_b` job.

Same runner as Pipeline A's (`runner.run_pipeline`), with B's stages and files: `stages_b.json` and
`quality_b.json`. B reads what A made (landmarks, A's features and the sync); when A has remade any
of that, B is stale and must be run again from `features_b` (a later stage alone would mix old and
new inputs). A run that completes `features_b` and all later stages clears the stale mark.
"""

import logging
from collections.abc import Mapping
from typing import Any

from app.pipeline_b import (
    B_STAGE_NAMES,
    NEEDED_FROM_A,
    QUALITY_B_FILE,
    STAGES_B_FILE,
    clear_stale,
    stale_info,
    weights_refusal,
)
from app.storage import webcam_path
from worker.pipeline.features_b import compute_features_b
from worker.pipeline.landmarks import LANDMARKS_FILE
from worker.pipeline.model_b import GazeModelB
from worker.pipeline.plots import run_plots
from worker.pipeline.quality_b import build_quality_b
from worker.pipeline.runner import (
    Context,
    StageFn,
    calibration_stage,
    gaze_stage,
    run_pipeline,
    select_stages,
)
from worker.pipeline.spec import SPEC_A, SPEC_B

log = logging.getLogger("pipeline")


class PipelineBError(Exception):
    """B cannot run on this session as asked (a precondition, not a crash of a stage)."""


def _features_b(ctx: Context) -> dict[str, Any]:
    if ctx.models_root is None or ctx.b_model is None:
        raise PipelineBError("the context has no weights directory or model name")
    reason = weights_refusal(ctx.models_root, ctx.b_model)
    if reason is not None:
        raise PipelineBError(reason)
    model = GazeModelB.load(
        ctx.models_root, ctx.b_model, provider="cuda" if ctx.onnx_provider == "cuda" else "cpu"
    )  # noqa: E501
    summary = compute_features_b(
        webcam_path(ctx.root, ctx.study_id, ctx.session_id),
        ctx.derived / LANDMARKS_FILE,
        ctx.derived / SPEC_A.features_file,
        ctx.derived / SPEC_B.features_file,
        model,
        {"name": model.name, "sha256": model.sha256, "provider": ctx.onnx_provider},
        ctx.config,
    )
    return summary.as_dict() | {"model": model.name}


def _calibration_b(ctx: Context) -> dict[str, Any]:
    return calibration_stage(ctx, SPEC_B)


def _gaze_b(ctx: Context) -> dict[str, Any]:
    return gaze_stage(ctx, SPEC_B)


def _quality_b(ctx: Context) -> dict[str, Any]:
    report = build_quality_b(ctx.root, ctx.study_id, ctx.session_id, ctx.config, ctx.stages)
    return {"included": report["inclusion"]["included"], "reasons": report["inclusion"]["reasons"]}


def _plots_b(ctx: Context) -> dict[str, Any]:
    return run_plots(ctx.root, ctx.study_id, ctx.session_id, ctx.config, SPEC_B).as_dict()


STAGES_B: dict[str, StageFn] = {
    "features_b": _features_b,
    "calibration_b": _calibration_b,
    "gaze_b": _gaze_b,
    "quality_b": _quality_b,
    "plots_b": _plots_b,
}
assert tuple(STAGES_B) == B_STAGE_NAMES  # the names are defined once, in `app.pipeline_b`


def run_pipeline_b(
    ctx: Context,
    from_stage: str | None = None,
    only: str | None = None,
    stages: Mapping[str, StageFn] = STAGES_B,
) -> dict[str, dict[str, Any]]:
    """Run B's stages. Raises `PipelineBError` when A has not made B's inputs or B is stale and the
    selection does not start at `features_b`; `StageFailed` when a stage raises."""
    selected = select_stages(list(stages), from_stage, only)
    missing = [n for n in NEEDED_FROM_A if not (ctx.derived / n).is_file()]
    if missing:
        raise PipelineBError(f"Pipeline A has not made {', '.join(missing)} for this session")
    stale = stale_info(ctx.derived)
    if stale is not None and selected[0] != "features_b":
        raise PipelineBError(
            f"Pipeline B is stale (Pipeline A ran '{stale['by_stage']}' after it); "
            "run it again from features_b"
        )
    result = run_pipeline(
        ctx, from_stage, only, stages,
        stages_file=STAGES_B_FILE, quality_file=QUALITY_B_FILE,
        quality_stages=("quality_b", "plots_b"), marks_b_stale=False,
    )  # fmt: skip
    if selected == list(B_STAGE_NAMES):  # all of B was made again from fresh inputs
        clear_stale(ctx.derived)
    return result
