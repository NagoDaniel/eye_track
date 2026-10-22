"""Export a session to a ZIP and import it into another instance (PLAN §12.3).

A field problem becomes reproducible on a developer machine: `export-session` writes the session's
files and its chunk table, `import-session` puts them back under a local `DATA_ROOT` with the
database rows (study, session, chunks), so the stages can be run again and the admin pages show it.

Layout of the ZIP:
    meta.json     the session row and study-config snapshot (the `raw/meta.json` of the session)
    chunks.json   the rows of the `chunks` table of the session: stream, seq, size, sha256
    raw/...       the raw files (chunks, assembled streams, webcam.webm), without meta.json
    derived/...   everything the stages wrote

The archive contains the participant's webcam video unless it was made with `include_raw_video`
false; treat it as personal data (PLAN §10).
"""

import hashlib
import json
import secrets
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session as Db

from app.models import Chunk, ChunkStream, SessionStatus, Study, StudyMode, now
from app.models import Session as SessionRow
from app.pipeline_b import summary_of
from app.schemas import StudyConfig
from app.storage import atomic_writer, meta_path, session_dir

TOP_LEVEL = ("meta.json", "chunks.json")
TREES = ("raw", "derived")


class ArchiveError(Exception):
    """The session cannot be exported, or the archive cannot be imported; the message says why."""


@dataclass(frozen=True)
class ArchiveSummary:
    files: int
    bytes: int
    contains_raw_video: bool


def _is_raw_video(relative: PurePosixPath) -> bool:
    return relative == PurePosixPath("raw/webcam.webm") or relative.parts[:3] == (
        "raw",
        "chunks",
        "webcam",
    )


def export_session(
    db: Db, root: Path, session_id: uuid.UUID, out: Path, include_raw_video: bool = True
) -> ArchiveSummary:
    """Write the ZIP `out` (atomically). Raises `ArchiveError` for an unknown session."""
    session = db.get(SessionRow, session_id)
    if session is None:
        raise ArchiveError(f"session {session_id} does not exist")
    directory = session_dir(root, session.study_id, session.id)
    meta_file = meta_path(root, session.study_id, session.id)
    if not meta_file.is_file():
        raise ArchiveError(f"{meta_file} is missing; the session has no data on disk")
    rows = db.scalars(
        select(Chunk).where(Chunk.session_id == session.id).order_by(Chunk.stream, Chunk.seq)
    ).all()
    chunks = [
        {"stream": r.stream.value, "seq": r.seq, "size": r.size, "sha256": r.sha256} for r in rows
    ]
    files = 0
    total = 0
    has_video = False
    with atomic_writer(out) as target, zipfile.ZipFile(target, "w") as archive:
        archive.writestr("meta.json", meta_file.read_bytes(), zipfile.ZIP_DEFLATED)
        archive.writestr("chunks.json", json.dumps(chunks, indent=0) + "\n", zipfile.ZIP_DEFLATED)
        for path in sorted(p for p in directory.rglob("*") if p.is_file()):
            relative = PurePosixPath(path.relative_to(directory).as_posix())
            if relative == PurePosixPath("raw/meta.json") or path.name.startswith("."):
                continue  # meta.json is at the top; dot files are temporary files
            if _is_raw_video(relative):
                if not include_raw_video:
                    continue
                has_video = True
            # Videos and gzip chunks are compressed already.
            stored = _is_raw_video(relative) or path.suffix in (".bin", ".gz", ".parquet", ".png")
            archive.write(
                path, str(relative), zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
            )
            files += 1
            total += path.stat().st_size
    return ArchiveSummary(files + 2, total, has_video)


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """The file members, after checking that none can write outside the session directory."""
    members = []
    for info in archive.infolist():
        if info.is_dir():
            continue
        name = PurePosixPath(info.filename)
        if name.is_absolute() or ".." in name.parts or "\\" in info.filename:
            raise ArchiveError(f"unsafe path in the archive: {info.filename!r}")
        if str(name) not in TOP_LEVEL and name.parts[0] not in TREES:
            raise ArchiveError(f"unexpected file in the archive: {info.filename!r}")
        members.append(info)
    for needed in TOP_LEVEL:
        if needed not in {i.filename for i in members}:
            raise ArchiveError(f"the archive has no {needed}")
    return members


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _unique_code(db: Db, study_id: int) -> str:
    code = f"imported-{study_id}"
    suffix = 1
    while db.scalar(select(func.count()).select_from(Study).where(Study.code == code)):
        suffix += 1
        code = f"imported-{study_id}-{suffix}"
    return code


def import_session(db: Db, root: Path, archive_path: Path, force: bool = False) -> uuid.UUID:
    """Put the archive's session under `root` and into the database.

    The study is created from the config snapshot when its id is not there. A session that already
    exists is an error unless `force`, which replaces it (rows and files). The archive is extracted
    and checked first, next to the target and under a temporary name; chunk files are checked
    against `chunks.json` (webcam chunks may be missing: an export without raw video). Only when
    everything is fine is anything of an existing session removed. Raises `ArchiveError` and then
    changes neither files nor database.
    """
    try:
        archive = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ArchiveError(f"{archive_path} is not a readable ZIP archive: {exc}") from exc
    with archive:
        members = _safe_members(archive)
        meta: dict[str, Any] = json.loads(archive.read("meta.json"))
        chunk_rows: list[dict[str, Any]] = json.loads(archive.read("chunks.json"))
        try:
            session_id, study_id = uuid.UUID(meta["id"]), int(meta["study_id"])
            config = StudyConfig.model_validate(meta["config"])
        except (KeyError, ValueError) as exc:
            raise ArchiveError(f"meta.json is not a session's: {exc}") from exc

        existing = db.get(SessionRow, session_id)
        directory = session_dir(root, study_id, session_id)
        if (existing is not None or directory.exists()) and not force:
            raise ArchiveError(
                f"session {session_id} already exists here; use --force to replace it"
            )
        staging = directory.with_name(f".import-{uuid.uuid4().hex}")
        try:
            for info in members:
                name = PurePosixPath(info.filename)
                if str(name) == "chunks.json":
                    continue
                target = staging / ("raw/meta.json" if str(name) == "meta.json" else str(name))
                with archive.open(info) as source, atomic_writer(target) as out:
                    shutil.copyfileobj(source, out)
            kept = _verify_chunks(staging, chunk_rows)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    replaced: Path | None = None
    try:
        if existing is not None:
            db.execute(delete(Chunk).where(Chunk.session_id == session_id))
            db.delete(existing)
            db.flush()
        if directory.exists():
            replaced = directory.with_name(f".replaced-{uuid.uuid4().hex}")
            directory.rename(replaced)
        staging.rename(directory)
        _add_rows(db, meta, config, directory, kept)
    except BaseException:
        # Put everything back as it was; the caller rolls the database back.
        if replaced is not None:
            shutil.rmtree(directory, ignore_errors=True)
            replaced.rename(directory)
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if replaced is not None:
        shutil.rmtree(replaced, ignore_errors=True)
    return session_id


def _add_rows(
    db: Db, meta: dict[str, Any], config: StudyConfig, directory: Path, kept: list[dict[str, Any]]
) -> None:
    """The study (if new), the session and its chunk rows."""
    session_id, study_id = uuid.UUID(meta["id"]), int(meta["study_id"])
    study = db.get(Study, study_id)
    if study is None:
        db.add(
            Study(
                id=study_id,
                name=f"Imported study {study_id}",
                code=_unique_code(db, study_id),
                mode=StudyMode.REMOTE,
                config=config.model_dump(),
            )
        )
        db.flush()
        # An explicit id does not move the sequence: later studies would collide with it.
        db.execute(
            text(
                "select setval(pg_get_serial_sequence('studies', 'id'), "
                "greatest((select max(id) from studies), 1))"
            )
        )
    report_file = directory / "derived" / "quality.json"
    report = json.loads(report_file.read_text("utf-8")) if report_file.is_file() else None
    b_line = summary_of(directory / "derived")  # Pipeline B's line in the record, from its files
    if report is not None and b_line is not None:
        report = {**report, "pipeline_b": b_line}
    has_webcam = (directory / "raw" / "webcam.webm").is_file() or any(
        r["stream"] == "webcam" for r in kept
    )
    db.add(
        SessionRow(
            id=session_id,
            study_id=study_id,
            participant_code=str(meta["participant_code"]),
            upload_token_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(),  # unusable
            status=SessionStatus.PROCESSED if report is not None else SessionStatus.UPLOADED,
            consent_version=meta.get("consent_version"),
            consent_at=_when(meta.get("consent_at")),
            client_info=meta.get("client_info"),
            quality=report,
            raw_video_deleted_at=None if has_webcam else now(),
            is_debug=bool(meta.get("is_debug")),
            flags=list(meta.get("flags", [])),
            created_at=_when(meta.get("created_at")) or now(),
        )
    )
    db.flush()
    for r in kept:
        db.add(Chunk(session_id=session_id, stream=ChunkStream(r["stream"]), seq=r["seq"],
                     size=r["size"], sha256=r["sha256"]))  # fmt: skip
    db.flush()


def _when(value: object) -> datetime | None:
    return datetime.fromisoformat(value) if isinstance(value, str) else None


def _verify_chunks(directory: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows whose file is there and matches; a missing webcam chunk is allowed (no raw video),
    anything else missing or changed is an error."""
    kept: list[dict[str, Any]] = []
    for r in rows:
        path = directory / "raw" / "chunks" / r["stream"] / f"{int(r['seq']):06d}.bin"
        if not path.is_file():
            if r["stream"] == "webcam":
                continue
            raise ArchiveError(
                f"{r['stream']} chunk {r['seq']} is in chunks.json but not in the archive"
            )
        if path.stat().st_size != r["size"] or _sha256(path) != r["sha256"]:
            raise ArchiveError(f"{r['stream']} chunk {r['seq']} does not match chunks.json")
        kept.append(r)
    return kept
