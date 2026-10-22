"""Export a whole study to one ZIP and import it into another instance (PLAN §12.3).

The ZIP holds what the study is made of:
    study.json              the study row (name, code, mode, config), its videos (the rows, with
                            their AOIs) and the sessions in the archive
    videos/{id}/video.mp4   the stimulus videos of the playlist
    sessions/{id}.zip       one session archive each, as `export-session` writes it (stored, not
                            compressed again), so a session is exported and imported by the same
                            code and checks (`session_archive`)
    analysis/...            the analysis and comparison runs of the study (optional)

Debug sessions are left out unless asked for. The archive holds the participants' webcam video
unless made without it: treat it as personal data (PLAN §10). Import creates the study, its videos
and its sessions under the ids they had, and refuses when the study id or code, or a video id with
another file, is already there; if anything fails, nothing is left behind.
"""

import hashlib
import json
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session as Db

from app.models import Session as SessionRow
from app.models import SessionStatus, Study, StudyMode, Video, now
from app.schemas import StudyConfig
from app.session_archive import ArchiveError, export_session, import_session
from app.storage import (
    atomic_writer,
    meta_path,
    video_aois_path,
    video_dir,
    video_path,
)

STUDY_FORMAT = 1
STUDY_FILE = "study.json"
EXPORTED = (
    SessionStatus.UPLOADED,
    SessionStatus.PROCESSING,
    SessionStatus.PROCESSED,
    SessionStatus.FAILED,
)


@dataclass(frozen=True)
class StudyArchiveSummary:
    sessions: int
    videos: int
    analysis_files: int
    bytes: int
    left_out_debug: int  # debug sessions not exported
    left_out_no_data: int  # sessions that never uploaded anything (or were withdrawn)


def _video_row(video: Video) -> dict[str, Any]:
    return {
        "id": video.id, "title": video.title, "duration": video.duration, "width": video.width,
        "height": video.height, "fps": video.fps, "sha256": video.sha256, "aois": video.aois,
    }  # fmt: skip


def export_study(
    db: Db,
    root: Path,
    study_id: int,
    out: Path,
    include_raw_video: bool = True,
    include_debug: bool = False,
    include_analysis: bool = True,
) -> StudyArchiveSummary:
    """Write the ZIP `out` (atomically). Raises `ArchiveError` for an unknown study or a session
    that cannot be exported."""
    study = db.get(Study, study_id)
    if study is None:
        raise ArchiveError(f"study {study_id} does not exist")
    config = StudyConfig.model_validate(study.config)
    videos = [v for v in (db.get(Video, int(i)) for i in config.playlist) if v is not None]
    sessions = db.scalars(
        select(SessionRow).where(SessionRow.study_id == study_id).order_by(SessionRow.created_at)
    ).all()
    chosen: list[SessionRow] = []
    left_debug = left_no_data = 0
    for s in sessions:
        if s.status not in EXPORTED or not meta_path(root, study_id, s.id).is_file():
            left_no_data += 1
        elif s.is_debug and not include_debug:
            left_debug += 1
        else:
            chosen.append(s)

    total = analysis_files = 0
    with (
        tempfile.TemporaryDirectory(prefix="study-export-") as scratch,
        atomic_writer(out) as target,
        zipfile.ZipFile(target, "w", allowZip64=True) as archive,
    ):
        document = {
            "format": STUDY_FORMAT,
            "id": study.id, "name": study.name, "code": study.code, "mode": study.mode.value,
            "config": study.config, "created_at": study.created_at.isoformat(),
            "videos": [_video_row(v) for v in videos],
            "sessions": [str(s.id) for s in chosen],
            "contains_raw_video": include_raw_video,
        }  # fmt: skip
        archive.writestr(STUDY_FILE, json.dumps(document, indent=1) + "\n")
        for v in videos:
            file = video_path(root, v.id)
            if not file.is_file():
                raise ArchiveError(f"the stimulus video {v.id} is not on disk ({file})")
            archive.write(file, f"videos/{v.id}/video.mp4", zipfile.ZIP_STORED)
            total += file.stat().st_size
        for s in chosen:
            part = Path(scratch) / f"{s.id}.zip"
            summary = export_session(db, root, s.id, part, include_raw_video)
            archive.write(part, f"sessions/{s.id}.zip", zipfile.ZIP_STORED)
            total += summary.bytes
            part.unlink()  # one session at a time on the scratch disk
        if include_analysis:
            tree = root / "analysis" / str(study_id)
            for path in sorted(p for p in tree.rglob("*") if p.is_file()) if tree.is_dir() else []:
                archive.write(path, f"analysis/{path.relative_to(tree).as_posix()}")
                analysis_files += 1
                total += path.stat().st_size
    return StudyArchiveSummary(len(chosen), len(videos), analysis_files, total, left_debug,
                               left_no_data)  # fmt: skip


def _safe_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        raise ArchiveError(f"unsafe path in the archive: {name!r}")
    return path


def _session_id(value: object) -> str:
    """A session id from `study.json` in its canonical form, or `ArchiveError`."""
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        raise ArchiveError(
            f"{STUDY_FILE} lists a session id that is not a UUID: {value!r}"
        ) from None


def _check_session_part(part: Path, session_id: str, study_id: int, member: str) -> None:
    """The nested session archive belongs to this session and this study (it names both itself)."""
    try:
        with zipfile.ZipFile(part) as nested:
            meta = json.loads(nested.read("meta.json"))
        found = (str(uuid.UUID(str(meta["id"]))), int(meta["study_id"]))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile) as exc:
        raise ArchiveError(f"{member} is not a session archive: {exc}") from exc
    if found != (session_id, study_id):
        raise ArchiveError(
            f"{member} holds session {found[0]} of study {found[1]}, not session {session_id} "
            f"of study {study_id}"
        )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def import_study(db: Db, root: Path, archive_path: Path) -> int:
    """Create the study, its videos and its sessions from the archive; returns the study id. The
    caller commits. Raises `ArchiveError` (and then leaves no file behind; the caller rolls the
    database back)."""
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArchiveError(f"{archive_path} is not a readable ZIP archive: {exc}") from exc
    created: list[Path] = []  # what this import made on disk, removed again on failure
    with archive, tempfile.TemporaryDirectory(prefix="study-import-") as scratch:
        names = {i.filename for i in archive.infolist() if not i.is_dir()}
        for name in names:
            _safe_name(name)
            top = PurePosixPath(name).parts[0]
            if name != STUDY_FILE and top not in ("videos", "sessions", "analysis"):
                raise ArchiveError(f"unexpected file in the archive: {name!r}")
        if STUDY_FILE not in names:
            raise ArchiveError(f"the archive has no {STUDY_FILE}: it is not a study archive")
        try:
            document = json.loads(archive.read(STUDY_FILE))
            if document["format"] != STUDY_FORMAT:
                raise ArchiveError(f"unknown study archive format {document['format']}")
            study_id = int(document["id"])
            config = StudyConfig.model_validate(document["config"])
            mode = StudyMode(document["mode"])
        except (KeyError, ValueError) as exc:
            raise ArchiveError(f"{STUDY_FILE} is not a study's: {exc}") from exc
        if db.get(Study, study_id) is not None:
            raise ArchiveError(f"a study with id {study_id} is already here")
        if db.scalar(select(Study).where(Study.code == document["code"])) is not None:
            raise ArchiveError(f"a study with the code '{document['code']}' is already here")

        try:
            _add_study(db, document, config, mode)
            _add_videos(db, root, archive, document["videos"], created)
            for listed in document["sessions"]:
                session_id = _session_id(listed)  # a UUID: it becomes part of two paths
                member = f"sessions/{session_id}.zip"
                if member not in names:
                    raise ArchiveError(f"{member} is listed in {STUDY_FILE} but not in the archive")
                part = Path(scratch) / f"{session_id}.zip"
                with archive.open(member) as source, part.open("wb") as out:
                    shutil.copyfileobj(source, out)
                _check_session_part(part, session_id, study_id, member)
                directory = root / "studies" / str(study_id) / "sessions" / session_id
                if directory.exists():
                    raise ArchiveError(f"session {session_id} already has files here")
                created.append(directory)
                imported = import_session(db, root, part, force=False)
                if str(imported) != session_id:
                    raise ArchiveError(f"{member} holds session {imported}")
                part.unlink()
            _copy_analysis(archive, names, root, study_id, created)
        except BaseException:
            for path in created:
                shutil.rmtree(path, ignore_errors=True)
            raise
    return study_id


def _add_study(db: Db, document: dict[str, Any], config: StudyConfig, mode: StudyMode) -> None:
    created = document.get("created_at")
    db.add(
        Study(
            id=int(document["id"]),
            name=str(document["name"]),
            code=str(document["code"]),
            mode=mode,
            config=config.model_dump(),
            created_at=datetime.fromisoformat(created) if isinstance(created, str) else now(),
        )  # fmt: skip
    )
    db.flush()
    # An explicit id does not move the sequence: later studies would collide with it.
    db.execute(
        text(
            "select setval(pg_get_serial_sequence('studies', 'id'), "
            "greatest((select max(id) from studies), 1))"
        )
    )


def _add_videos(
    db: Db, root: Path, archive: zipfile.ZipFile, rows: list[dict[str, Any]], created: list[Path]
) -> None:
    for row in rows:
        video_id = int(row["id"])
        member = f"videos/{video_id}/video.mp4"
        try:
            info = archive.getinfo(member)
        except KeyError:
            raise ArchiveError(
                f"{member} is listed in {STUDY_FILE} but not in the archive"
            ) from None
        existing = db.get(Video, video_id)
        if existing is not None:
            if existing.sha256 != row["sha256"]:
                raise ArchiveError(f"video {video_id} is here with another file")
            continue  # the same video: use it
        target = video_path(root, video_id)
        if target.exists():
            raise ArchiveError(f"video {video_id} already has a file here")
        created.append(video_dir(root, video_id))
        with archive.open(info) as source, atomic_writer(target) as out:
            shutil.copyfileobj(source, out)
        if _sha256(target) != row["sha256"]:
            raise ArchiveError(f"video {video_id} does not match its sha256")
        if row.get("aois") is not None:
            video_aois_path(root, video_id).write_text(json.dumps(row["aois"], indent=2))
        db.add(
            Video(
                id=video_id,
                title=str(row["title"]),
                file_path=f"videos/{video_id}/video.mp4",
                duration=float(row["duration"]),
                width=int(row["width"]),
                height=int(row["height"]),
                fps=float(row["fps"]),
                sha256=str(row["sha256"]),
                aois=row.get("aois"),
            )  # fmt: skip
        )
    db.flush()
    db.execute(
        text(
            "select setval(pg_get_serial_sequence('videos', 'id'), "
            "greatest((select coalesce(max(id), 1) from videos), 1))"
        )
    )


def _copy_analysis(
    archive: zipfile.ZipFile, names: set[str], root: Path, study_id: int, created: list[Path]
) -> None:
    members = sorted(n for n in names if n.startswith("analysis/"))
    if not members:
        return
    base = root / "analysis" / str(study_id)
    if base.exists():
        raise ArchiveError(f"{base} already exists here")
    created.append(base)
    for name in members:
        target = base / PurePosixPath(name).relative_to("analysis")
        with archive.open(name) as source, atomic_writer(target) as out:
            shutil.copyfileobj(source, out)
