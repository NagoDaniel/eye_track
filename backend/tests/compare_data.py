"""Sessions with known Pipeline A and B numbers for the comparison tests: the quality files and the
database rows are written directly (the comparison only reads those)."""

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db

from app.models import Session as SessionRow
from app.models import SessionStatus, Study, StudyMode
from app.storage import derived_dir


@dataclass(frozen=True)
class Numbers:
    accuracy_norm: float | None
    corrected_norm: float | None
    accuracy_px: float | None
    precision_px: float | None
    valid_fraction: float | None


def report(n: Numbers, calibration: str = "ok") -> dict[str, Any]:
    """A quality report (A's or B's) holding the numbers the comparison reads."""
    validation = None
    if n.accuracy_norm is not None:
        validation = {
            "blocks": [{"mean_error_px": n.accuracy_px}, {"mean_error_px": n.accuracy_px}],
            "mean_error_norm": n.accuracy_norm,
            "mean_error_corrected_norm": n.corrected_norm,
            "precision_px": n.precision_px,
        }
    return {
        "calibration": {"status": calibration},
        "validation": validation,
        "playback": None if n.valid_fraction is None else {"valid_fraction": n.valid_fraction},
        "inclusion": {"included": False, "reasons": ["validation_error_high"]},  # A's, ignored
    }


def add_study(engine: Engine, study_id: int = 1) -> None:
    with Db(engine) as db:
        db.add(Study(id=study_id, name="S", code=f"s{study_id}", mode=StudyMode.REMOTE,
                     config={"playlist": []}))  # fmt: skip
        db.commit()


def add_session(
    engine: Engine,
    root: Path,
    code: str,
    a: Numbers | None,
    b: Numbers | None,
    *,
    study_id: int = 1,
    status: SessionStatus = SessionStatus.PROCESSED,
    is_debug: bool = False,
    stale: bool = False,
    calibration_a: str = "ok",
    calibration_b: str = "ok",
    write_files: bool = True,
) -> uuid.UUID:
    """A session with A's numbers `a` and B's `b` (None: B never run)."""
    sid = uuid.uuid4()
    derived = derived_dir(root, study_id, sid)
    derived.mkdir(parents=True)
    quality: dict[str, Any] = report(a or Numbers(None, None, None, None, None), calibration_a)
    if b is not None:
        quality["pipeline_b"] = {"included": True, "reasons": [], "model": "m", "stale": stale,
                                 "calibration_status": calibration_b}  # fmt: skip
        if write_files:
            (derived / "quality_b.json").write_text(json.dumps(report(b, calibration_b)))
    if write_files:
        (derived / "quality.json").write_text(json.dumps(quality))
    with Db(engine) as db:
        db.add(SessionRow(id=sid, study_id=study_id, participant_code=code, upload_token_hash="h",
                          status=status, quality=quality, is_debug=is_debug, flags=[]))  # fmt: skip
        db.commit()
    return sid
