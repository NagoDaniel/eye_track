"""Queueing an analysis run (PLAN §6, §9) on real PostgreSQL."""

from collections.abc import Iterator

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db

from app.analysis_runs import analysis_jobs, queue_analysis
from app.models import JobStatus, Study, StudyMode, Video
from app.service_errors import ConflictError, InvalidError, NotFoundError


@pytest.fixture
def db(migrated_engine: Engine) -> Iterator[Db]:
    with Db(migrated_engine) as session:
        for n in (1, 2):
            session.add(Study(id=n, name=f"S{n}", code=f"c{n}", mode=StudyMode.REMOTE, config={}))
            session.add(Video(id=n, title="V", file_path="x", duration=1.0, width=2, height=1,
                              fps=25.0, sha256="0" * 64))  # fmt: skip
        session.commit()
        yield session


def test_queueing_records_the_options_and_a_run_id(db: Db) -> None:
    job, run_id = queue_analysis(db, 1, 2, "drift_corrected", 0.5, True)
    db.commit()
    assert job.args == {"study_id": 1, "video_id": 2, "run_id": run_id,
                        "gaze_columns": "drift_corrected", "aoi_tolerance_k": 0.5,
                        "heatmap_dots": True}  # fmt: skip
    assert job.status is JobStatus.QUEUED
    assert [j.id for j in analysis_jobs(db, study_id=1, video_id=2)] == [job.id]
    assert analysis_jobs(db, study_id=2) == [] and analysis_jobs(db, video_id=1) == []


def test_unknown_study_or_video_and_bad_options_are_refused(db: Db) -> None:
    with pytest.raises(NotFoundError):
        queue_analysis(db, 9, 1)
    with pytest.raises(NotFoundError):
        queue_analysis(db, 1, 9)
    for bad in (
        {"gaze_columns": "raw"},
        {"aoi_tolerance_k": -1.0},
        {"aoi_tolerance_k": float("nan")},
    ):
        with pytest.raises(InvalidError):
            queue_analysis(db, 1, 1, **bad)
    assert analysis_jobs(db) == []


def test_a_second_run_of_the_same_video_waits_for_the_first(db: Db) -> None:
    first, _ = queue_analysis(db, 1, 1)
    db.commit()
    with pytest.raises(ConflictError):
        queue_analysis(db, 1, 1)
    queue_analysis(db, 1, 2)  # another video is fine
    queue_analysis(db, 2, 1)  # so is another study
    first.status = JobStatus.DONE
    db.commit()
    queue_analysis(db, 1, 1)  # the first one is over
