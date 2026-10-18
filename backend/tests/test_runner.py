"""The stage runner (PLAN §8): stage order, re-runs, failures, a failed calibration."""

import json
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
import pytest

from tests.session_fixture import FixtureSession, copy_fixture_session
from worker.pipeline.config import PipelineConfig
from worker.pipeline.quality import QUALITY_FILE
from worker.pipeline.runner import (
    STAGE_NAMES,
    STAGES,
    Context,
    StageFailed,
    pipeline_flags,
    run_pipeline,
    select_stages,
)

CONFIG = PipelineConfig()
DERIVED_TABLES = (
    "webcam_frames.parquet",
    "landmarks.parquet",
    "features_a.parquet",
    "gaze_a.parquet",
    "fixations_a.parquet",
)


def context(s: FixtureSession, model: Path, config: PipelineConfig = CONFIG) -> Context:
    return Context(
        root=s.root, study_id=s.study_id, session_id=s.session_id, config=config, model=model,
        max_inflated_bytes=32 * 1024 * 1024, manifest=s.manifest,
    )  # fmt: skip


def derived_of(s: FixtureSession) -> Path:
    return s.raw.parent / "derived"


@pytest.fixture(scope="module")
def processed(tmp_path_factory: pytest.TempPathFactory, face_model: Path) -> FixtureSession:
    """The recorded session after a full run; tests that change things work on a copy."""
    s = copy_fixture_session(tmp_path_factory.mktemp("processed"))
    run_pipeline(context(s, face_model))
    return s


@pytest.fixture
def copy_of(processed: FixtureSession, tmp_path: Path) -> FixtureSession:
    shutil.copytree(processed.root, tmp_path / "data")
    return replace(
        processed,
        root=tmp_path / "data",
        raw=tmp_path / "data" / processed.raw.relative_to(processed.root),
    )


def read(path: Path) -> pd.DataFrame:
    """A table as a data frame (pandas, unlike Arrow's `equals`, treats NaN as equal to NaN)."""
    frame: pd.DataFrame = pq.read_table(path).to_pandas()
    return frame


# --- selection ---


def test_stage_selection() -> None:
    names = list(STAGE_NAMES)
    assert select_stages(names, None, None) == names
    assert select_stages(names, "sync", None) == ["sync", "calibration", "gaze", "quality"]
    assert select_stages(names, None, "features") == ["features"]
    with pytest.raises(ValueError, match="unknown stage 'nope'"):
        select_stages(names, "nope", None)
    with pytest.raises(ValueError, match="not both"):
        select_stages(names, "sync", "sync")


def test_the_real_stages_are_in_the_order_of_the_plan() -> None:
    assert list(STAGES) == list(STAGE_NAMES)


# --- a full run on the recorded session ---


def test_a_full_run_produces_every_file_and_records_every_stage(processed: FixtureSession) -> None:
    derived = derived_of(processed)
    for name in (*DERIVED_TABLES, "sync.json", "calibration_a.json", QUALITY_FILE):
        assert (derived / name).exists(), name
    assert (processed.raw / "webcam.webm").exists()
    report = json.loads((derived / QUALITY_FILE).read_text("utf-8"))
    assert list(report["stages"]) == list(STAGE_NAMES)
    for name, entry in report["stages"].items():
        assert entry["seconds"] >= 0 and isinstance(entry["summary"], dict), name
    assert report["stages"]["ingest"]["summary"]["webcam_frames"] > 200
    assert report["stages"]["landmarks"]["summary"]["frames_with_face"] > 200
    assert report["stages"]["sync"]["summary"]["flags"] == ["sync_short"]
    assert report["stages"]["calibration"]["summary"]["status"] == "ok"
    assert report["stages"]["gaze"]["summary"]["frames"] > 200
    assert report["stages"]["quality"]["summary"]["included"] is False  # the fake camera
    assert pipeline_flags(derived) == ["sync_short"]


def test_running_again_gives_the_same_result(copy_of: FixtureSession, face_model: Path) -> None:
    derived = derived_of(copy_of)
    before = {name: read(derived / name) for name in DERIVED_TABLES}
    quality_before = json.loads((derived / QUALITY_FILE).read_text("utf-8"))
    run_pipeline(context(copy_of, face_model))
    for name, table in before.items():
        assert read(derived / name).equals(table), name
    quality_after = json.loads((derived / QUALITY_FILE).read_text("utf-8"))
    for report in (quality_before, quality_after):
        report.pop("stages")
    assert quality_after == quality_before


def test_from_a_stage_the_earlier_ones_are_left_alone(
    copy_of: FixtureSession, face_model: Path
) -> None:
    derived = derived_of(copy_of)
    old = json.loads((derived / QUALITY_FILE).read_text("utf-8"))["stages"]
    untouched = {
        n: (derived / n).stat().st_mtime_ns for n in ("landmarks.parquet", "features_a.parquet")
    }
    sync_before = (derived / "sync.json").stat().st_mtime_ns

    stages = run_pipeline(context(copy_of, face_model), from_stage="sync")

    assert {n: (derived / n).stat().st_mtime_ns for n in untouched} == untouched
    assert (derived / "sync.json").stat().st_mtime_ns > sync_before
    # The earlier stages keep what their last run recorded; the later ones have new timings.
    for name in ("ingest", "landmarks", "features"):
        assert stages[name] == old[name]
    assert list(stages) == list(STAGE_NAMES)
    assert (
        json.loads((derived / QUALITY_FILE).read_text("utf-8"))["stages"]["quality"]["seconds"] >= 0
    )


def test_one_stage_only(copy_of: FixtureSession, face_model: Path) -> None:
    derived = derived_of(copy_of)
    quality_before = (derived / QUALITY_FILE).stat().st_mtime_ns
    gaze_before = (derived / "gaze_a.parquet").stat().st_mtime_ns
    features_before = (derived / "features_a.parquet").stat().st_mtime_ns
    run_pipeline(context(copy_of, face_model), only="features")
    assert (derived / "features_a.parquet").stat().st_mtime_ns > features_before
    assert (derived / QUALITY_FILE).stat().st_mtime_ns == quality_before  # not rebuilt
    assert (derived / "gaze_a.parquet").stat().st_mtime_ns == gaze_before


def test_a_failing_stage_stops_the_run_and_leaves_the_earlier_files(
    copy_of: FixtureSession, face_model: Path
) -> None:
    derived = derived_of(copy_of)
    (derived / "landmarks.parquet").unlink()
    features_before = read(derived / "features_a.parquet")
    with pytest.raises(StageFailed, match="stage 'features' failed") as caught:
        run_pipeline(context(copy_of, face_model), from_stage="features")
    assert caught.value.stage == "features" and isinstance(
        caught.value.__cause__, FileNotFoundError
    )
    assert read(derived / "features_a.parquet").equals(features_before)  # not touched
    assert (derived / "webcam_frames.parquet").exists() and (copy_of.raw / "webcam.webm").exists()


def test_ingest_without_the_chunk_table_rows_fails_loudly(
    copy_of: FixtureSession, face_model: Path
) -> None:
    ctx = replace(context(copy_of, face_model), manifest=None)
    with pytest.raises(StageFailed, match="chunk table"):
        run_pipeline(ctx, only="ingest")


# --- a failed calibration ---


def test_a_failed_calibration_skips_the_gaze_and_excludes_the_session(
    copy_of: FixtureSession, face_model: Path
) -> None:
    derived = derived_of(copy_of)
    impossible = replace(CONFIG, calib_min_frames_per_dot=10_000)
    stages = run_pipeline(context(copy_of, face_model, impossible), from_stage="calibration")

    assert stages["calibration"]["summary"]["status"] == "failed"
    assert stages["gaze"]["summary"] == {"skipped": "calibration_failed"}
    assert (
        not (derived / "gaze_a.parquet").exists() and not (derived / "fixations_a.parquet").exists()
    )
    report = json.loads((derived / QUALITY_FILE).read_text("utf-8"))
    assert report["inclusion"]["included"] is False
    assert report["inclusion"]["reasons"] == ["calibration_failed"]
    assert pipeline_flags(derived) == ["sync_short", "calibration_failed"]

    # A later run that fits again brings the gaze back and the flag goes.
    run_pipeline(context(copy_of, face_model), from_stage="calibration")
    assert (derived / "gaze_a.parquet").exists()
    assert pipeline_flags(derived) == ["sync_short"]


# --- orchestration with stand-in stages (the real ones are tested above) ---


def test_stages_run_in_order_with_their_results_visible_to_later_ones(tmp_path: Path) -> None:
    import uuid

    ctx = Context(root=tmp_path, study_id=1, session_id=uuid.UUID(int=1), config=CONFIG,
                  model=tmp_path, max_inflated_bytes=1)  # fmt: skip
    seen: list[str] = []

    def make(name: str) -> Any:
        def stage(c: Context) -> dict[str, Any]:
            seen.append(f"{name}:{sorted(c.stages)}")
            return {"name": name}

        return stage

    stages = {n: make(n) for n in ("a", "b", "c")}
    result = run_pipeline(ctx, stages=stages)
    assert seen == ["a:[]", "b:['a']", "c:['a', 'b']"]
    assert [result[n]["summary"] for n in "abc"] == [{"name": "a"}, {"name": "b"}, {"name": "c"}]
