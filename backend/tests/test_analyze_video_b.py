"""An analysis with Pipeline B (PLAN §9; DECISIONS 2026-10-20): the same outputs from B's gaze,
B's accuracy and B's inclusion; sessions without a fresh B are left out with the reason.

The session with the real small gaze network (`b_session`) is copied as three participants whose
gaze (A's and B's) differs by known offsets in x, as in `test_analyze_video`."""

import csv
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db

from app.models import JobStatus
from app.models import Session as SessionRow
from app.storage import analysis_run_dir
from tests.analysis_data import VIDEO_ID, Participant, make_participants, seed
from tests.session_fixture import FixtureSession
from tests.test_analyze_video import OFFSETS, WHOLE_VIDEO, record_of, run_job


@pytest.fixture
def world_b(
    migrated_engine: Engine, b_session: FixtureSession, tmp_path: Path
) -> tuple[Path, list[Participant]]:
    root = tmp_path / "data"
    people = make_participants(b_session, root, OFFSETS)
    seed(migrated_engine, b_session, root, people, WHOLE_VIDEO)
    return root, people


def test_pipeline_b_gives_the_same_outputs_from_its_own_gaze(
    migrated_engine: Engine, world_b: tuple[Path, list[Participant]]
) -> None:
    root, _ = world_b
    job, run_id = run_job(migrated_engine, root, pipeline="b")
    assert job.status is JobStatus.DONE, job.log
    record = record_of(root, run_id)
    assert record["parameters"]["pipeline"] == "b" and record["excluded"] == []
    assert sorted(p["participant_code"] for p in record["included"]) == ["P1", "P2", "P3"]
    run_dir = analysis_run_dir(root, 1, VIDEO_ID, run_id)
    assert {"heatmap.mp4", "density.npz", "synchrony.csv", "aoi_metrics_wide.csv"} <= set(
        record["outputs"]
    )
    # B's gaze was shifted by the offsets: the dispersion is the known one
    assert record["results"]["synchrony"]["mean_dispersion"] == pytest.approx(0.1 / 3, abs=1e-6)
    with (run_dir / "aoi_metrics_wide.csv").open() as f:
        assert sorted({r["participant_code"] for r in csv.DictReader(f)}) == ["P1", "P2", "P3"]


def test_the_accuracy_that_sets_the_heatmap_sigma_is_b_s_not_a_s(
    migrated_engine: Engine, world_b: tuple[Path, list[Participant]]
) -> None:
    root, _ = world_b
    _, run_a = run_job(migrated_engine, root, pipeline="a")
    _, run_b = run_job(migrated_engine, root, pipeline="b")
    a = {p["participant_code"]: p["accuracy_px"] for p in record_of(root, run_a)["included"]}
    b = {p["participant_code"]: p["accuracy_px"] for p in record_of(root, run_b)["included"]}
    derived = root / "studies" / "1" / "sessions"
    for p in record_of(root, run_b)["included"]:
        quality_b = json.loads(
            (derived / p["session_id"] / "derived" / "quality_b.json").read_text()
        )
        blocks = quality_b["validation"]["blocks"]
        corrected = [
            x["mean_error_corrected_px"]
            for x in blocks
            if x["mean_error_corrected_px"] is not None  # an exact 0.0 counts (as in the analysis)
        ]
        assert b[p["participant_code"]] == pytest.approx(sum(corrected) / len(corrected))
    assert a != b  # the two pipelines do not have the same accuracy on this recording


def set_b_line(engine: Engine, session_id: uuid.UUID, change: dict[str, Any] | None) -> None:
    """Change (or, with None, remove) Pipeline B's line in a session's record."""
    with Db(engine) as db:
        row = db.get_one(SessionRow, session_id)
        quality = dict(row.quality or {})
        if change is None:
            quality.pop("pipeline_b", None)
        else:
            quality["pipeline_b"] = {**quality["pipeline_b"], **change}
        row.quality = quality
        db.commit()


def test_sessions_without_a_usable_b_are_left_out_with_the_reason(
    migrated_engine: Engine, world_b: tuple[Path, list[Participant]]
) -> None:
    root, people = world_b
    set_b_line(migrated_engine, people[0].session_id, None)  # B never run
    set_b_line(migrated_engine, people[1].session_id, {"stale": True})
    job, run_id = run_job(migrated_engine, root, pipeline="b")
    assert job.status is JobStatus.DONE, job.log
    record = record_of(root, run_id)
    assert [p["participant_code"] for p in record["included"]] == ["P3"]
    left = {e["participant_code"]: e["reasons"] for e in record["excluded"]}
    assert left == {"P1": ["pipeline_b_not_run"], "P2": ["pipeline_b_stale"]}


def test_b_has_its_own_inclusion_decision(
    migrated_engine: Engine, world_b: tuple[Path, list[Participant]]
) -> None:
    root, people = world_b
    set_b_line(migrated_engine, people[0].session_id,
               {"included": False, "reasons": ["validation_error_high"]})  # fmt: skip
    _, run_b = run_job(migrated_engine, root, pipeline="b")
    left = {e["participant_code"]: e["reasons"] for e in record_of(root, run_b)["excluded"]}
    assert left == {"P1": ["validation_error_high"]}
    _, run_a = run_job(migrated_engine, root, pipeline="a")  # A's decision is untouched
    assert record_of(root, run_a)["excluded"] == []


def test_a_missing_b_gaze_file_is_reported(
    migrated_engine: Engine, world_b: tuple[Path, list[Participant]]
) -> None:
    root, people = world_b
    path = root / "studies" / "1" / "sessions" / str(people[0].session_id) / "derived"
    (path / "gaze_b.parquet").unlink()
    _, run_id = run_job(migrated_engine, root, pipeline="b")
    left = {e["participant_code"]: e["reasons"] for e in record_of(root, run_id)["excluded"]}
    assert left == {"P1": ["gaze_file_missing"]}


def test_an_unknown_pipeline_is_refused(migrated_engine: Engine) -> None:
    from app.analysis_runs import queue_analysis
    from app.models import Study, StudyMode, Video
    from app.service_errors import InvalidError

    with Db(migrated_engine) as db:
        db.add(Study(id=1, name="S", code="c", mode=StudyMode.REMOTE, config={}))
        db.add(Video(id=1, title="V", file_path="x", duration=1.0, width=2, height=1,
                     fps=25.0, sha256="0" * 64))  # fmt: skip
        db.commit()
        with pytest.raises(InvalidError, match="pipeline must be one of"):
            queue_analysis(db, 1, 1, pipeline="c")
