"""The `analyze_video` job and the CLI on the recorded session, processed for real (PLAN §9, §13).

The recorded session goes through the real pipeline once (real ffmpeg and MediaPipe) and is copied
as three participants whose gaze differs by known offsets in x (-0.05, 0 and +0.05 of the video
width). Their gaze is otherwise identical, so the dispersion is known: (0.05 + 0 + 0.05) / 3.
"""

import csv
import json
import uuid
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db
from typer.testing import CliRunner

import cli
import worker.analysis.analyze as analyze_module
from app.analysis_runs import RUN_FILE, new_run_id, queue_analysis
from app.db import make_session_factory
from app.models import Job, JobKind, JobStatus, SessionStatus, Video
from app.models import Session as SessionRow
from app.storage import analysis_run_dir, derived_dir
from tests.analysis_data import (
    VIDEO_ID,
    Participant,
    make_participants,
    process_fixture,
    seed,
)
from tests.dbutil import make_settings
from tests.session_fixture import FixtureSession
from worker.analysis.config import AnalysisConfig
from worker.analysis.run import start_run
from worker.analysis.select import Selection
from worker.handlers import make_analyze_video
from worker.run import process_one

OFFSETS = {"P1": -0.05, "P2": 0.0, "P3": 0.05}
WHOLE_VIDEO = {
    "segments": [{"id": "start", "start": 0.0, "end": 3.0}],
    "aois": [{"id": "frame", "label": "Whole frame", "start": 0.0, "end": 21.8, "shape": "rect",
              "coords": [-1.0, -1.0, 2.0, 2.0]}],
}  # fmt: skip
runner = CliRunner()


@pytest.fixture(scope="module")
def processed(tmp_path_factory: pytest.TempPathFactory, face_model: Path) -> FixtureSession:
    return process_fixture(tmp_path_factory.mktemp("analysis_source"), face_model)


@pytest.fixture
def world(
    migrated_engine: Engine, processed: FixtureSession, tmp_path: Path
) -> tuple[Path, list[Participant]]:
    root = tmp_path / "data"
    people = make_participants(processed, root, OFFSETS)
    seed(migrated_engine, processed, root, people, WHOLE_VIDEO)
    return root, people


def run_job(engine: Engine, root: Path, **args: Any) -> tuple[Job, str]:
    factory = make_session_factory(engine)
    with factory() as db:
        job, run_id = queue_analysis(db, 1, VIDEO_ID, **args)
        db.commit()
        job_id = job.id
    handler = make_analyze_video(factory, make_settings(data_root=root))
    assert process_one(factory, {JobKind.ANALYZE_VIDEO: handler}) is True
    with factory() as db:
        return db.get_one(Job, job_id), run_id


def record_of(root: Path, run_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (analysis_run_dir(root, 1, VIDEO_ID, run_id) / RUN_FILE).read_text()
    )
    return data


def test_a_run_writes_every_output_and_the_known_dispersion(
    migrated_engine: Engine, world: tuple[Path, list[Participant]]
) -> None:
    root, people = world
    job, run_id = run_job(migrated_engine, root)
    assert job.status is JobStatus.DONE, job.log
    run_dir = analysis_run_dir(root, 1, VIDEO_ID, run_id)
    record = record_of(root, run_id)

    assert record["status"] == "done" and record["error"] is None and record["finished_at"]
    assert sorted(p["participant_code"] for p in record["included"]) == ["P1", "P2", "P3"]
    assert record["excluded"] == []
    assert record["parameters"]["gaze_columns"] == "drift_corrected"
    for name in record["outputs"]:
        assert (run_dir / name).is_file() and (run_dir / name).stat().st_size > 0, name
    assert set(record["outputs"]) == {
        "heatmap.mp4", "density.npz", "static/001_start.png", "aoi_metrics_long.csv",
        "aoi_metrics_wide.csv", "synchrony.csv", "synchrony_nss.csv", "synchrony.png",
    }  # fmt: skip
    assert not list(run_dir.rglob("*.tmp"))

    # identical samples, shifted: the same count for everyone, dispersion 0.0333 in every window
    samples = {p["samples"] for p in record["included"]}
    assert (
        len(samples) == 1 and samples.pop() >= 10
    )  # the fake camera's face rarely stays inside the viewport
    sync = record["results"]["synchrony"]
    assert sync["mean_dispersion"] == pytest.approx(0.1 / 3, abs=1e-6)
    assert sync["windows_with_two_or_more"] >= 2
    with (run_dir / "synchrony.csv").open() as f:
        rows = [r for r in csv.DictReader(f) if r["dispersion"]]
    assert rows and all(float(r["dispersion"]) == pytest.approx(0.1 / 3, abs=1e-6) for r in rows)

    # every participant against the one AOI; the AOI covers everything, so all samples are inside
    with (run_dir / "aoi_metrics_wide.csv").open() as f:
        wide = list(csv.DictReader(f))
    assert sorted(r["participant_code"] for r in wide) == ["P1", "P2", "P3"]
    assert all(
        float(r["dwell_sample_s"]) > 0.2 and r["time_to_first_fixation_s"] != "" for r in wide
    )
    assert record["results"]["heatmap_video"]["frames"] == 545
    assert record["results"]["sigma"]["sessions_used"] == 3


def test_the_run_options_are_recorded_and_used(
    migrated_engine: Engine, world: tuple[Path, list[Participant]]
) -> None:
    root, _ = world
    job, run_id = run_job(
        migrated_engine, root, gaze_columns="drift_corrected", aoi_tolerance_k=1.5
    )
    assert job.status is JobStatus.DONE, job.log
    record = record_of(root, run_id)
    assert record["parameters"]["gaze_columns"] == "drift_corrected"
    assert record["parameters"]["aoi_tolerance_k"] == 1.5
    assert record["results"]["aoi_metrics"]["tolerance_k"] == 1.5


def test_left_out_sessions_are_listed_with_their_reasons(
    migrated_engine: Engine, world: tuple[Path, list[Participant]], processed: FixtureSession
) -> None:
    root, people = world
    with Db(migrated_engine) as db:
        db.add(SessionRow(id=uuid.UUID(int=0xB001), study_id=1, participant_code="P4",
                          upload_token_hash="h", status=SessionStatus.PROCESSED,
                          quality=db.get_one(SessionRow, people[0].session_id).quality,
                          flags=[]))  # fmt: skip
        db.add(SessionRow(id=uuid.UUID(int=0xB002), study_id=1, participant_code="P5",
                          upload_token_hash="h", status=SessionStatus.UPLOADED,
                          flags=[]))  # fmt: skip
        db.commit()
    job, run_id = run_job(migrated_engine, root)
    assert job.status is JobStatus.DONE, job.log
    left = {e["participant_code"]: e["reasons"] for e in record_of(root, run_id)["excluded"]}
    assert left == {"P4": ["gaze_file_missing"], "P5": ["not_processed"]}


def test_no_included_session_fails_the_job_and_says_why(
    migrated_engine: Engine, world: tuple[Path, list[Participant]]
) -> None:
    root, people = world
    with Db(migrated_engine) as db:
        for p in people:
            db.get_one(SessionRow, p.session_id).status = SessionStatus.FAILED
        db.commit()
    job, run_id = run_job(migrated_engine, root)
    assert job.status is JobStatus.FAILED and "no included sessions" in job.log
    record = record_of(root, run_id)
    assert record["status"] == "failed" and "no included sessions" in record["error"]
    assert {e["reasons"][0] for e in record["excluded"]} == {"not_processed"}
    assert record["outputs"] == []


def test_a_failing_stage_keeps_the_finished_outputs(
    migrated_engine: Engine, world: tuple[Path, list[Participant]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = world

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no room on disk")

    monkeypatch.setattr(analyze_module, "render_static", boom)
    job, run_id = run_job(migrated_engine, root)
    assert job.status is JobStatus.FAILED and "heatmap_static: no room on disk" in job.log
    record = record_of(root, run_id)
    assert record["status"] == "failed" and record["error"] == "heatmap_static: no room on disk"
    assert record["outputs"] == ["heatmap.mp4", "density.npz"]
    run_dir = analysis_run_dir(root, 1, VIDEO_ID, run_id)
    assert (run_dir / "heatmap.mp4").is_file() and not (run_dir / "static").exists()


def test_a_missing_gaze_file_leaves_that_session_out_and_the_run_goes_on(
    migrated_engine: Engine, world: tuple[Path, list[Participant]]
) -> None:
    root, people = world
    gaze = (
        root
        / "studies"
        / "1"
        / "sessions"
        / str(people[0].session_id)
        / "derived"
        / "fixations_a.parquet"
    )
    gaze.write_bytes(b"not a parquet file")
    job, run_id = run_job(migrated_engine, root)
    assert job.status is JobStatus.DONE, job.log
    record = record_of(root, run_id)
    assert [p["participant_code"] for p in record["included"]] == ["P2", "P3"]
    (left,) = record["excluded"]
    assert left["participant_code"] == "P1" and left["reasons"][0].startswith("gaze_unreadable")


def test_the_cli_runs_an_analysis_and_lists_the_files(
    migrated_engine: Engine, world: tuple[Path, list[Participant]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _ = world
    settings = make_settings(data_root=root)
    monkeypatch.setattr(cli, "settings", lambda: settings)
    monkeypatch.setattr(cli, "session_factory", lambda: make_session_factory(migrated_engine))
    monkeypatch.setattr(cli, "open_session", lambda: make_session_factory(migrated_engine)())
    result = runner.invoke(cli.app, ["analyze", "1", str(VIDEO_ID), "--tolerance-k", "1", "--dots"])
    assert result.exit_code == 0, result.output
    assert "done, 3 participants" in result.output
    assert "heatmap.mp4" in result.output and "synchrony.png" in result.output
    (run_dir,) = (root / "analysis" / "1" / str(VIDEO_ID)).iterdir()
    parameters = json.loads((run_dir / RUN_FILE).read_text())["parameters"]
    assert parameters["aoi_tolerance_k"] == 1.0 and parameters["heatmap_dots"] is True
    assert parameters["gaze_columns"] == "drift_corrected"  # the default
    measured = runner.invoke(cli.app, ["analyze", "1", str(VIDEO_ID), "--as-measured"])
    assert measured.exit_code == 0, measured.output
    newest = max((root / "analysis" / "1" / str(VIDEO_ID)).iterdir())
    assert json.loads((newest / RUN_FILE).read_text())["parameters"]["gaze_columns"] == "plain"

    with Db(migrated_engine) as db:  # an open job for the same video: refused
        queue_analysis(db, 1, VIDEO_ID)
        db.commit()
    again = runner.invoke(cli.app, ["analyze", "1", str(VIDEO_ID)])
    assert again.exit_code == 1 and "already queued or running" in again.output


def test_new_run_ids_are_unique() -> None:
    assert len({new_run_id() for _ in range(50)}) == 50


def test_a_requeued_run_starts_again_from_nothing(
    migrated_engine: Engine, world: tuple[Path, list[Participant]]
) -> None:
    """The worker died during the first attempt: its directory holds a `running` record and a file.
    The requeued job (same run id) must not trip over them."""
    root, _ = world
    factory = make_session_factory(migrated_engine)
    with factory() as db:
        job, run_id = queue_analysis(db, 1, VIDEO_ID)
        db.commit()
        job_id = job.id
    _, run_dir = start_run(root, 1, VIDEO_ID, run_id, AnalysisConfig(), Selection([], []))
    (run_dir / "stale.csv").write_text("left behind")
    handler = make_analyze_video(factory, make_settings(data_root=root))
    assert process_one(factory, {JobKind.ANALYZE_VIDEO: handler}) is True
    with factory() as db:
        assert db.get_one(Job, job_id).status is JobStatus.DONE
    assert record_of(root, run_id)["status"] == "done" and not (run_dir / "stale.csv").exists()


def test_a_finished_run_is_never_overwritten(
    migrated_engine: Engine, world: tuple[Path, list[Participant]]
) -> None:
    root, _ = world
    factory = make_session_factory(migrated_engine)
    with factory() as db:
        job, run_id = queue_analysis(db, 1, VIDEO_ID)
        db.commit()
    record, run_dir = start_run(root, 1, VIDEO_ID, run_id, AnalysisConfig(), Selection([], []))
    record.finish(run_dir)
    handler = make_analyze_video(factory, make_settings(data_root=root))
    assert process_one(factory, {JobKind.ANALYZE_VIDEO: handler}) is True
    with factory() as db:
        failed = db.get_one(Job, job.id)
    assert failed.status is JobStatus.FAILED and "already finished" in failed.log
    assert record_of(root, run_id)["status"] == "done"


def test_a_run_id_that_queue_analysis_did_not_make_is_refused(
    migrated_engine: Engine, world: tuple[Path, list[Participant]]
) -> None:
    root, _ = world
    handler = make_analyze_video(
        make_session_factory(migrated_engine), make_settings(data_root=root)
    )
    for bad in ("../x", "x", 5):
        args = {"study_id": 1, "video_id": VIDEO_ID, "run_id": bad}
        with pytest.raises(ValueError, match="run_id"):
            handler(Job(kind=JobKind.ANALYZE_VIDEO, args=args))
    assert not (root / "analysis").exists()


def test_a_video_without_areas_still_gets_heatmaps_and_synchrony(
    migrated_engine: Engine, world: tuple[Path, list[Participant]]
) -> None:
    root, _ = world
    with Db(migrated_engine) as db:
        db.get_one(Video, VIDEO_ID).aois = None
        db.commit()
    job, run_id = run_job(migrated_engine, root)
    assert job.status is JobStatus.DONE, job.log
    record = record_of(root, run_id)
    assert not any(n.startswith("aoi_metrics") for n in record["outputs"])
    assert record["results"]["aoi_metrics"]["skipped"] == "the video has no AOIs"
    # no segments either: windows of 10 s over the 21.8 s video
    assert [n for n in record["outputs"] if n.startswith("static/")] == [
        "static/001_window_1.png", "static/002_window_2.png", "static/003_window_3.png",
    ]  # fmt: skip
    assert "synchrony.csv" in record["outputs"] and "heatmap.mp4" in record["outputs"]


def test_when_every_drift_corrected_column_is_empty_the_run_says_so(
    migrated_engine: Engine, world: tuple[Path, list[Participant]]
) -> None:
    root, people = world
    for p in people:
        path = derived_dir(root, 1, p.session_id) / "gaze_a.parquet"
        table = pq.read_table(path)
        for name in ("x_vid_dc", "y_vid_dc"):
            nothing = pa.nulls(len(table), pa.float32())
            table = table.set_column(table.schema.get_field_index(name), name, nothing)
        pq.write_table(table.replace_schema_metadata(pq.read_schema(path).metadata), path)
    job, run_id = run_job(migrated_engine, root, gaze_columns="drift_corrected")
    assert job.status is JobStatus.FAILED and "no included sessions" in job.log
    left = record_of(root, run_id)["excluded"]
    assert sorted(e["participant_code"] for e in left) == ["P1", "P2", "P3"]
    assert all(e["reasons"] == ["no_valid_samples"] for e in left)
    plain, _ = run_job(
        migrated_engine, root, gaze_columns="plain"
    )  # the plain columns are untouched
    assert plain.status is JobStatus.DONE


def test_the_dots_option_reaches_the_run(
    migrated_engine: Engine, world: tuple[Path, list[Participant]]
) -> None:
    root, _ = world
    job, run_id = run_job(migrated_engine, root, heatmap_dots=True)
    assert job.status is JobStatus.DONE, job.log
    assert record_of(root, run_id)["parameters"]["heatmap_dots"] is True
