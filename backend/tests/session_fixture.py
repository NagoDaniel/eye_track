"""The real recorded session used by the pipeline tests (`shared/fixtures/session_e2e/`).

It was captured by the e2e test (a real Chromium, MediaRecorder and MediaPipe in the browser; the
fake camera played `e2e/fixtures/face.mp4`) and is copied into a temporary `DATA_ROOT` so a test can
run a stage on it without touching the committed files.
"""

import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from worker.pipeline.ingest import ChunkRecord

FIXTURE = Path(__file__).resolve().parents[2] / "shared" / "fixtures" / "session_e2e"


@dataclass(frozen=True)
class FixtureSession:
    root: Path
    study_id: int
    session_id: uuid.UUID
    manifest: list[ChunkRecord]
    raw: Path


def load_manifest() -> list[ChunkRecord]:
    rows = json.loads((FIXTURE / "chunks.json").read_text(encoding="utf-8"))
    return [ChunkRecord(r["stream"], r["seq"], r["size"], r["sha256"]) for r in rows]


def copy_fixture_session(tmp_path: Path) -> FixtureSession:
    meta = json.loads((FIXTURE / "raw" / "meta.json").read_text(encoding="utf-8"))
    study_id, session_id = int(meta["study_id"]), uuid.UUID(meta["id"])
    raw = tmp_path / "studies" / str(study_id) / "sessions" / str(session_id) / "raw"
    shutil.copytree(FIXTURE / "raw", raw)
    return FixtureSession(tmp_path, study_id, session_id, load_manifest(), raw)
