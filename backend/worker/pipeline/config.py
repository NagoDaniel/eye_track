"""Thresholds of the processing pipeline (PLAN §1: configuration over constants).

Every number the plan names for a stage is a field here with the plan's default; a stage takes the
config as an argument and writes the values it used into its output (DECISIONS, milestone 3 item
10). Starting values the plan leaves to tuning are marked as such. Not editable per study.
"""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class PipelineConfig:
    # --- Pipeline A features (PLAN §8.4) ---
    # blink = eyeBlink blendshape above this in either eye ...
    blink_score_threshold: float = 0.5
    # ... or an eye openness (lid distance / eye width) below this. Starting value, tune on
    # calibration data: in the face clip blinks measure 0.07 to 0.14 and open eyes 0.27 to 0.39.
    blink_openness_threshold: float = 0.15

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
