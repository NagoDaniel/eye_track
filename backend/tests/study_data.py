"""A whole study on disk and in the database for the study archive tests: two participants and a
debug session (the recorded, processed session copied under other ids), a stimulus video with
AOIs and an analysis run."""

import hashlib
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine, delete
from sqlalchemy.orm import Session as Db

from app.models import Chunk, ChunkStream, Study, Video
from app.models import Session as SessionRow
from app.storage import meta_path, session_dir
from tests.analysis_data import make_participants, seed
from tests.analysis_fake import make_run
from tests.session_fixture import FixtureSession

AOIS = {"segments": [], "aois": [{"id": "a", "label": "A", "start": 0, "end": 2, "shape": "rect",
                                  "coords": [0.1, 0.1, 0.5, 0.5]}]}  # fmt: skip


@dataclass(frozen=True)
class StudyOnDisk:
    root: Path
    study_id: int
    participants: dict[str, uuid.UUID]  # code -> session id (the debug one is "DBG")
    video_sha256: str
    run_id: str


def build_study(engine: Engine, source: FixtureSession, root: Path) -> StudyOnDisk:
    people = make_participants(source, root, {"P1": 0.0, "P2": 0.05, "DBG": -0.05})
    seed(engine, source, root, people, AOIS)
    debug = next(p for p in people if p.code == "DBG")
    video_sha = hashlib.sha256((root / "videos" / "1" / "video.mp4").read_bytes()).hexdigest()
    with Db(engine) as db:
        db.get_one(Video, 1).sha256 = video_sha  # as a real upload has it
        study = db.get_one(Study, source.study_id)
        study.name, study.code, study.config = "Spring study", "spring", {"playlist": ["1"]}
        for p in people:  # the chunk rows the export checks against
            for r in source.manifest:
                db.add(Chunk(session_id=p.session_id, stream=ChunkStream(r.stream), seq=r.seq,
                             size=r.size, sha256=r.sha256))  # fmt: skip
        db.get_one(SessionRow, debug.session_id).is_debug = True
        db.commit()
    path = meta_path(root, source.study_id, debug.session_id)
    path.write_text(json.dumps({**json.loads(path.read_text()), "is_debug": True}))
    run_id = make_run(root, study_id=source.study_id, video_id=1, codes=("P1", "P2"))
    return StudyOnDisk(root, source.study_id, {p.code: p.session_id for p in people}, video_sha,
                       run_id)  # fmt: skip


def forget_everything(engine: Engine) -> None:
    """Empty the database, as on a machine that never had the study."""
    with Db(engine) as db:
        for table in (Chunk, SessionRow, Video, Study):
            db.execute(delete(table))
        db.commit()


def files_of(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def session_files(root: Path, study_id: int, session_id: uuid.UUID) -> dict[str, str]:
    return files_of(session_dir(root, study_id, session_id))
