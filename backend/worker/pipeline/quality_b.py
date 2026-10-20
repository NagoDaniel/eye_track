"""Quality report of Pipeline B, `derived/quality_b.json`, and B's inclusion decision (PLAN §8.10).

The report of Pipeline A (`quality.json`) stays as it is. This one holds what is specific to B and
the numbers the comparison needs (§9.5): the features' provenance (the network and its weights),
the calibration fit, the valid fraction during playback, the validation accuracy and precision per
block with the drift, and whether the session is included in an analysis with B according to the
study's `inclusion` thresholds (the same numbers as for A), with the reasons when it is not.
Everything about the recording itself (webcam rate, face lost, events, sync) is in A's report.
"""

import json
import uuid
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from app.pipeline_b import QUALITY_B_FILE
from app.schemas import StudyConfig
from app.storage import atomic_write_bytes, derived_dir, meta_path
from worker.pipeline.config import PipelineConfig
from worker.pipeline.quality import decide_inclusion, playback_summary, validation_summary
from worker.pipeline.spec import SPEC_B
from worker.pipeline.validation import validate_session

QUALITY_B_VERSION = 1


def _features_info(derived: Path) -> dict[str, Any]:
    path = derived / SPEC_B.features_file
    metadata = pq.read_schema(path).metadata or {}
    info: dict[str, Any] = json.loads(metadata[b"features_b"])
    table = pq.read_table(path, columns=["face", "gaze_pitch"])
    face = [bool(f) for f in table["face"].to_pylist()]
    angle = table["gaze_pitch"].to_pylist()
    return {
        **info,
        "frames": table.num_rows,
        "frames_with_face": sum(face),
        "frames_with_angles": sum(1 for a in angle if a is not None),
    }


def build_quality_b(
    root: Path,
    study_id: int,
    session_id: uuid.UUID,
    config: PipelineConfig,
    stages: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the report from B's files and write `derived/quality_b.json`. With a failed
    calibration there is no gaze, so the playback and validation parts are null and the session is
    excluded for B. Raises if a file of an earlier stage is missing."""
    derived = derived_dir(root, study_id, session_id)
    meta = json.loads(meta_path(root, study_id, session_id).read_text(encoding="utf-8"))
    study_config = StudyConfig.model_validate(meta["config"])
    calibration = json.loads((derived / SPEC_B.calibration_file).read_text(encoding="utf-8"))
    ok = calibration["status"] == "ok"

    playback_part: dict[str, Any] | None = None
    validation_part: dict[str, Any] | None = None
    validation_error: float | None = None
    if ok:
        playback_part = playback_summary(pq.read_table(derived / SPEC_B.gaze_file))
        validation_part, validation_error = validation_summary(
            validate_session(root, study_id, session_id, config, spec=SPEC_B)
        )
    inclusion = decide_inclusion(
        is_debug=bool(meta.get("is_debug")),
        calibration_ok=ok,
        playback_frames=0 if playback_part is None else playback_part["frames"],
        valid_fraction=None if playback_part is None else playback_part["valid_fraction"],
        validation_error_norm=validation_error,
        inclusion=study_config.inclusion,
    )
    report: dict[str, Any] = {
        "version": QUALITY_B_VERSION,
        "pipeline": SPEC_B.id,
        "config": config.as_dict(),
        "is_debug": bool(meta.get("is_debug")),
        "features": _features_info(derived),
        "calibration": {
            "status": calibration["status"],
            "failed_reason": calibration["failed_reason"],
            "feature_columns": calibration["feature_columns"],
            "attempt_used": calibration.get("attempt_used"),
            "cv": calibration.get("cv"),
            "samples": calibration.get("samples"),
        },
        "playback": playback_part,
        "validation": validation_part,
        "inclusion": inclusion.as_dict(),
        "stages": stages or {},
    }
    atomic_write_bytes(derived / QUALITY_B_FILE, (json.dumps(report, indent=1) + "\n").encode())
    return report
