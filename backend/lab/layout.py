"""Where the lab keeps its files, and which session files it copies.

`lab_data/` (repository root, gitignored; `LAB_DATA` overrides it):

    sessions/                      a data root in the layout of DATA_ROOT (app/storage.py)
      studies/<study>/sessions/<uuid>/
        raw/                       the session's raw files, read-only (no upload chunks)
        derived/                   cached outputs that are slow to make (landmarks, features_b)
        reference/                 the product pipeline's own outputs, for comparison
    results/<experiment>/
      data/                        a data root per experiment: raw/ links to the session's copy,
                                   derived/ holds links to the cached files and this run's outputs
      scores.json, report.txt
"""

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.pipeline_b import CALIBRATION_B_FILE, FEATURES_B_FILE, GAZE_B_FILE
from app.storage import meta_path
from worker.pipeline.ingest import WEBCAM_FRAMES_FILE
from worker.pipeline.landmarks import LANDMARKS_FILE
from worker.pipeline.spec import SPEC_A
from worker.pipeline.sync import SYNC_FILE

REPO_ROOT = Path(__file__).resolve().parents[2]
DOT_PATH_FILE = REPO_ROOT / "e2e" / "fixtures" / "moving_dot_path.json"
EXPERIMENTS_DIR = Path(__file__).resolve().parent / "experiments"

# Derived files an experiment starts from instead of recomputing them (MediaPipe on every frame,
# the gaze network on every face). They depend only on the raw video and the model files.
CACHED_FILES = (LANDMARKS_FILE, WEBCAM_FRAMES_FILE, FEATURES_B_FILE, "blendshapes.parquet")
# The product pipeline's outputs, kept to check that the lab reproduces them.
REFERENCE_FILES = (
    SYNC_FILE, SPEC_A.calibration_file, SPEC_A.gaze_file, CALIBRATION_B_FILE, GAZE_B_FILE,
    "quality.json",
)  # fmt: skip
# Upload chunks are already assembled into the raw files; the lab never needs them.
SKIPPED_RAW = ("chunks",)


def lab_data_root() -> Path:
    configured = os.environ.get("LAB_DATA")
    return Path(configured) if configured else REPO_ROOT / "lab_data"


def sessions_root(lab_data: Path) -> Path:
    return lab_data / "sessions"


def results_dir(lab_data: Path, experiment: str) -> Path:
    return lab_data / "results" / experiment


def reference_dir(root: Path, study_id: int, session_id: uuid.UUID) -> Path:
    return root / "studies" / str(study_id) / "sessions" / str(session_id) / "reference"


@dataclass(frozen=True)
class SessionRef:
    study_id: int
    session_id: uuid.UUID
    code: str  # participant code, from meta.json


def list_sessions(root: Path, study_id: int) -> list[SessionRef]:
    """The sessions of a study under a data root, ordered by session id."""
    base = root / "studies" / str(study_id) / "sessions"
    if not base.is_dir():
        return []
    refs = []
    for d in sorted(base.iterdir()):
        sid = uuid.UUID(d.name)
        meta = json.loads(meta_path(root, study_id, sid).read_text(encoding="utf-8"))
        refs.append(SessionRef(study_id, sid, str(meta["participant_code"])))
    return refs


LABELS_FILE = "labels.csv"  # in lab_data/: study,code,person,glasses,webcam,notes (gitignored)
LABEL_COLUMNS = ("study", "code", "person", "glasses", "webcam", "notes")


def read_labels(lab_data: Path) -> dict[tuple[int, str], str]:
    """(study, code) -> "person glasses webcam notes" from `lab_data/labels.csv`, when it exists.
    The file is written by hand (docs/T13_RECORDING.md); a wrong header fails loudly."""
    import csv

    path = lab_data / LABELS_FILE
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if tuple(reader.fieldnames or ()) != LABEL_COLUMNS:
            raise ValueError(f"{path}: the header must be {','.join(LABEL_COLUMNS)}")
        out = {}
        for row in reader:
            text = " ".join(
                f"{k}={row[k].strip()}" for k in LABEL_COLUMNS[2:] if row[k] and row[k].strip()
            )
            out[(int(row["study"]), row["code"].strip().upper())] = text
        return out
