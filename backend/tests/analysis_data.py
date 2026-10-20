"""Processed sessions for the analysis tests: the recorded session run through the real pipeline
(real ffmpeg and MediaPipe), copied as several participants whose gaze is shifted by known offsets.
"""

import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.compute as pc
import pyarrow.parquet as pq
from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db

from app.models import Session as SessionRow
from app.models import SessionStatus, Study, StudyMode, Video
from app.storage import derived_dir, meta_path, session_dir
from tests.session_fixture import FixtureSession, copy_fixture_session
from worker.pipeline.config import PipelineConfig
from worker.pipeline.gaze import FIXATIONS_FILE, GAZE_FILE
from worker.pipeline.quality import QUALITY_FILE
from worker.pipeline.runner import Context, run_pipeline

MOVING_DOT = Path(__file__).resolve().parents[2] / "e2e" / "fixtures" / "moving_dot.mp4"
VIDEO_ID = 1
VIDEO = {"duration": 21.8, "width": 1280, "height": 720, "fps": 25.0}
LOOSE_INCLUSION = {"max_validation_error_norm": 100.0, "min_valid_fraction": 0.0}


@dataclass(frozen=True)
class Participant:
    session_id: uuid.UUID
    code: str
    offset_x: float


def process_fixture(tmp_path: Path, model: Path) -> FixtureSession:
    """The recorded session after a full pipeline run, with inclusion thresholds that let it in
    (its calibration is not meant to be good: a fake camera played a face clip)."""
    s = copy_fixture_session(tmp_path)
    meta = json.loads(meta_path(s.root, s.study_id, s.session_id).read_text())
    meta["config"]["inclusion"] = LOOSE_INCLUSION
    meta_path(s.root, s.study_id, s.session_id).write_text(json.dumps(meta))
    run_pipeline(Context(
        root=s.root, study_id=s.study_id, session_id=s.session_id, config=PipelineConfig(),
        model=model, max_inflated_bytes=32 * 1024 * 1024, manifest=s.manifest,
    ))  # fmt: skip
    quality = json.loads((derived_dir(s.root, s.study_id, s.session_id) / QUALITY_FILE).read_text())
    assert quality["inclusion"]["included"], quality["inclusion"]
    return s


def _shift(path: Path, columns: list[str], offset: float) -> None:
    table = pq.read_table(path)
    for name in columns:
        i = table.schema.get_field_index(name)
        shifted = pc.add(table[name], offset)
        table = table.set_column(i, name, shifted)
    pq.write_table(table.replace_schema_metadata(pq.read_schema(path).metadata), path,
                   compression="snappy")  # fmt: skip


def make_participants(
    source: FixtureSession, root: Path, offsets: dict[str, float]
) -> list[Participant]:
    """Copy the processed session into `root` once per participant, with `x_vid` (and its
    drift-corrected twin, and the fixation centroids) moved by the participant's offset."""
    people = []
    for n, (code, offset) in enumerate(offsets.items(), start=1):
        sid = uuid.UUID(int=0xA000 + n)
        target = session_dir(root, source.study_id, sid)
        shutil.copytree(session_dir(source.root, source.study_id, source.session_id), target)
        meta = meta_path(root, source.study_id, sid)
        meta.write_text(json.dumps({**json.loads(meta.read_text()), "id": str(sid)}))
        derived = derived_dir(root, source.study_id, sid)
        _shift(derived / GAZE_FILE, ["x_vid", "x_vid_dc"], offset)
        _shift(derived / FIXATIONS_FILE, ["cx_vid", "cx_vid_dc"], offset)
        people.append(Participant(sid, code, offset))
    return people


def seed(
    engine: Engine,
    source: FixtureSession,
    root: Path,
    people: list[Participant],
    aois: dict[str, Any] | None = None,
) -> None:
    """Study, video (with its file under `root`) and the participants' sessions, `processed`."""
    quality = json.loads(
        (derived_dir(source.root, source.study_id, source.session_id) / QUALITY_FILE).read_text()
    )
    video_file = root / "videos" / str(VIDEO_ID) / "video.mp4"
    video_file.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(MOVING_DOT, video_file)
    with Db(engine) as db:
        db.add(Study(id=source.study_id, name="S", code="abc", mode=StudyMode.REMOTE, config={}))
        db.add(Video(id=VIDEO_ID, title="Dot", file_path=str(video_file), sha256="0" * 64,
                     aois=aois, **VIDEO))  # fmt: skip
        db.flush()
        for p in people:
            db.add(SessionRow(
                id=p.session_id, study_id=source.study_id, participant_code=p.code,
                upload_token_hash="h", status=SessionStatus.PROCESSED, quality=quality, flags=[],
            ))  # fmt: skip
        db.commit()


def add_pipeline_b(s: FixtureSession, model_root: Path, name: str = "l2cs_mobileone_s0") -> None:
    """Run Pipeline B on a processed session in place: the real network (small weights by default),
    then B's calibration and gaze stages."""
    from app.storage import webcam_path
    from worker.pipeline.features_b import compute_features_b
    from worker.pipeline.landmarks import LANDMARKS_FILE
    from worker.pipeline.model_b import GazeModelB
    from worker.pipeline.runner import calibration_stage, gaze_stage
    from worker.pipeline.spec import SPEC_A, SPEC_B

    config = PipelineConfig()
    derived = derived_dir(s.root, s.study_id, s.session_id)
    model = GazeModelB.load(model_root, name)
    compute_features_b(
        webcam_path(s.root, s.study_id, s.session_id),
        derived / LANDMARKS_FILE,
        derived / SPEC_A.features_file,
        derived / SPEC_B.features_file,
        model,
        {"name": model.name, "sha256": model.sha256},
        config,
    )
    ctx = Context(root=s.root, study_id=s.study_id, session_id=s.session_id, config=config,
                  model=Path("unused"), max_inflated_bytes=1)  # fmt: skip
    calibration_stage(ctx, SPEC_B)
    gaze_stage(ctx, SPEC_B)
