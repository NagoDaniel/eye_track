"""The analysis parameters equal the plan's numbers (PLAN §9) and the build records a git hash."""

import json

import pytest

from worker.analysis.config import AnalysisConfig, git_hash


def test_defaults_are_the_plan_values() -> None:
    c = AnalysisConfig()
    assert c.pipeline == "a"
    assert c.heatmap_window_s == 0.5  # §9.1: stim_time in [s - 0.5, s + 0.5]
    assert c.sigma_min == 0.03
    assert (c.grid_width, c.grid_height) == (64, 36)
    assert c.segment_default_s == 10.0  # §9.2
    assert c.aoi_tolerance_k == 0.0  # §9.3
    assert c.synchrony_window_ms == 200.0  # §9.4
    assert c.gaze_columns == "drift_corrected"  # DECISIONS 2026-10-20


def test_as_dict_is_plain_json() -> None:
    assert json.loads(json.dumps(AnalysisConfig().as_dict()))["grid_width"] == 64


def test_git_hash_comes_from_the_build_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_SHA", "abc123-dirty")
    assert git_hash() == "abc123-dirty"
    for unknown in ("", "  "):
        monkeypatch.setenv("GIT_SHA", unknown)
        assert git_hash() is None
    monkeypatch.delenv("GIT_SHA")
    assert git_hash() is None
