"""Copy processed sessions from a data root (normally `data/`) into the lab.

The source is only read. The copy's raw files are made read-only (raw data is immutable, PLAN §1),
so a lab run that tried to write into them would fail loudly.
"""

import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from app.storage import derived_dir, raw_dir
from lab.layout import CACHED_FILES, REFERENCE_FILES, SKIPPED_RAW, SessionRef, list_sessions
from lab.layout import reference_dir as reference_of
from worker.pipeline.landmarks import LANDMARKS_FILE


class LabImportError(Exception):
    """A session cannot be imported; the message says why."""


@dataclass(frozen=True)
class Imported:
    session: SessionRef
    status: str  # "imported", "exists" or "skipped: <reason>"


def _read_only(tree: Path) -> None:
    for path in tree.rglob("*"):
        if path.is_file():
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def import_session(source: Path, lab: Path, ref: SessionRef, force: bool = False) -> Imported:
    src_derived = derived_dir(source, ref.study_id, ref.session_id)
    if not (src_derived / LANDMARKS_FILE).is_file():
        return Imported(ref, "skipped: not processed (no landmarks.parquet)")
    target_raw = raw_dir(lab, ref.study_id, ref.session_id)
    session = target_raw.parent
    if session.exists():
        if not force:
            return Imported(ref, "exists")
        for path in session.rglob("*"):  # read-only files must be writable to be removed
            path.chmod(path.stat().st_mode | stat.S_IWUSR)
        shutil.rmtree(session)
    shutil.copytree(
        raw_dir(source, ref.study_id, ref.session_id), target_raw,
        ignore=shutil.ignore_patterns(*SKIPPED_RAW),
    )  # fmt: skip
    _read_only(target_raw)
    for names, target in (
        (CACHED_FILES, derived_dir(lab, ref.study_id, ref.session_id)),
        (REFERENCE_FILES, reference_of(lab, ref.study_id, ref.session_id)),
    ):
        target.mkdir(parents=True, exist_ok=True)
        for name in names:
            if (src_derived / name).is_file():
                shutil.copy2(src_derived / name, target / name)
    return Imported(ref, "imported")


def import_study(source: Path, lab: Path, study_id: int, force: bool = False) -> list[Imported]:
    """Every session of `study_id` under the data root `source`, into the lab's sessions root."""
    sessions = list_sessions(source, study_id)
    if not sessions:
        raise LabImportError(f"study {study_id} has no sessions under {source}")
    return [import_session(source, lab, ref, force) for ref in sessions]
