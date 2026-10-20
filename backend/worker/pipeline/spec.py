"""What differs between Pipeline A and Pipeline B once the features exist (PLAN §8.6 to §8.9).

Both pipelines fit the same per-session regression, predict, smooth, check the validation dots and
find fixations with the same code; they differ in the features (the table and the columns of the
feature vector) and so in the files they read and write. A `PipelineSpec` names those (PLAN §1: one
definition). A's file names are the ones of Milestones 2 and 3 and do not change.
"""

from dataclasses import dataclass

from app.pipeline_b import (
    CALIBRATION_B_FILE,
    FEATURES_B_FILE,
    FIXATIONS_B_FILE,
    GAZE_B_FILE,
)
from worker.pipeline.features_a import FEATURE_VECTOR_COLUMNS


@dataclass(frozen=True)
class PipelineSpec:
    id: str  # "a" or "b"
    features_file: str  # a table with `index`, `pts`, `face`, `blink` and the feature columns
    feature_columns: tuple[str, ...]  # the feature vector the regression is fitted on
    calibration_file: str
    gaze_file: str
    fixations_file: str


SPEC_A = PipelineSpec(
    id="a",
    features_file="features_a.parquet",
    feature_columns=FEATURE_VECTOR_COLUMNS,
    calibration_file="calibration_a.json",
    gaze_file="gaze_a.parquet",
    fixations_file="fixations_a.parquet",
)

# Pipeline B (PLAN §8.5, DECISIONS 2026-10-20): the gaze angles of the neural network plus the head
# pose and face size of A. Its blinks and validity are A's.
B_FEATURE_COLUMNS = ("gaze_pitch", "gaze_yaw", "yaw", "pitch", "roll", "face_ratio")

SPEC_B = PipelineSpec(
    id="b",
    features_file=FEATURES_B_FILE,
    feature_columns=B_FEATURE_COLUMNS,
    calibration_file=CALIBRATION_B_FILE,
    gaze_file=GAZE_B_FILE,
    fixations_file=FIXATIONS_B_FILE,
)

SPECS = {SPEC_A.id: SPEC_A, SPEC_B.id: SPEC_B}


def spec_of(pipeline: str) -> PipelineSpec:
    try:
        return SPECS[pipeline]
    except KeyError:
        raise ValueError(f"unknown pipeline '{pipeline}' (pipelines: {', '.join(SPECS)})") from None
