"""Parameters of the cross-participant analysis (PLAN §9).

Every number the plan names for an analysis is a field here with the plan's default; a run records
the values it used in `run.json` (PLAN §1: configuration over constants). Later tasks add the fields
they need.
"""

import os
from dataclasses import asdict, dataclass
from typing import Any, Literal

GazeColumns = Literal["plain", "drift_corrected"]


@dataclass(frozen=True)
class AnalysisConfig:
    # Which pipeline's gaze is analysed (PLAN §9: default A; B arrives with Milestone 5).
    pipeline: Literal["a"] = "a"
    # Smoothed gaze as it is, or with the drift correction of §8.8 (DECISIONS 2026-10-19 item 2).
    gaze_columns: GazeColumns = "plain"

    # --- Dynamic heatmap (PLAN §9.1) ---
    # Samples within this many seconds either side of a stimulus frame's time are used.
    heatmap_window_s: float = 0.5
    # Lower limit of the Gaussian sigma, in video-normalized units.
    sigma_min: float = 0.03
    # Resolution of the density grid and its `.npz` export.
    grid_width: int = 64
    grid_height: int = 36
    # One colour scale per video: this percentile of the non-zero densities of all frames
    # (DECISIONS 2026-10-19 item 5).
    heatmap_scale_percentile: float = 99.0
    # Opacity of the hottest colour; colder places are more transparent. Not in the plan.
    heatmap_alpha_max: float = 0.65
    # Also draw one dot per participant (the median of their samples in the window).
    heatmap_dots: bool = False

    # --- Static heatmaps (PLAN §9.2) ---
    # Time windows when `aois.json` has no segments.
    segment_default_s: float = 10.0

    # --- AOI metrics (PLAN §9.3) ---
    # Shapes are grown by k times the participant's validation accuracy.
    aoi_tolerance_k: float = 0.0

    # --- Attentional synchrony (PLAN §9.4) ---
    synchrony_window_ms: float = 200.0

    def as_dict(self) -> dict[str, Any]:
        """Plain JSON types, for `run.json`."""
        return asdict(self)


def git_hash() -> str | None:
    """The git hash the image was built from (`GIT_SHA` build argument), None when not known."""
    value = os.environ.get("GIT_SHA", "").strip()
    return value or None
