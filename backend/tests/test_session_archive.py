"""Export and import of a session (PLAN §12.3): a round trip on the recorded, processed session."""

import hashlib
import json
import shutil
import uuid
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Engine, delete, select
from sqlalchemy.orm import Session as Db

from app.models import Chunk, SessionStatus, Study, StudyMode
from app.models import Session as SessionRow
from app.session_archive import ArchiveError, ArchiveSummary, export_session, import_session
from tests.session_db import seed_session
from tests.session_fixture import FixtureSession, copy_fixture_session
from worker.pipeline.config import PipelineConfig
from worker.pipeline.ingest import ChunkRecord
from worker.pipeline.runner import Context, run_pipeline

CONFIG = PipelineConfig()


@pytest.fixture(scope="module")
def processed(tmp_path_factory: pytest.TempPathFactory, face_model: Path) -> FixtureSession:
    s = copy_fixture_session(tmp_path_factory.mktemp("archive"))
    run_pipeline(
        Context(s.root, s.study_id, s.session_id, CONFIG, face_model, 32 * 1024 * 1024, s.manifest)
    )
    return s


@pytest.fixture
def here(processed: FixtureSession, tmp_path: Path, migrated_engine: Engine) -> FixtureSession:
    """A copy of the processed session on disk and in the database (status `processed`)."""
    shutil.copytree(processed.root, tmp_path / "data")
    s = replace(
        processed,
        root=tmp_path / "data",
        raw=tmp_path / "data" / processed.raw.relative_to(processed.root),
    )
    seed_session(migrated_engine, s, SessionStatus.PROCESSED)
    return s


def files_of(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def session_directory(s: FixtureSession) -> Path:
    return s.raw.parent


def export(engine: Engine, s: FixtureSession, out: Path, raw_video: bool = True) -> ArchiveSummary:
    with Db(engine) as db:
        return export_session(db, s.root, s.session_id, out, raw_video)


def forget(engine: Engine, s: FixtureSession) -> None:
    """Remove the session from the database and the disk, as on a machine that never had it."""
    with Db(engine) as db:
        db.execute(delete(Chunk))
        db.execute(delete(SessionRow))
        db.execute(delete(Study))
        db.commit()
    shutil.rmtree(s.root)


def test_the_archive_holds_the_session_and_its_chunk_table(
    here: FixtureSession, migrated_engine: Engine, tmp_path: Path
) -> None:
    summary = export(migrated_engine, here, tmp_path / "s.zip")
    with zipfile.ZipFile(tmp_path / "s.zip") as z:
        names = z.namelist()
        assert names[:2] == ["meta.json", "chunks.json"]
        assert "raw/meta.json" not in names  # it is the top-level meta.json
        assert "raw/webcam.webm" in names and "raw/chunks/webcam/000000.bin" in names
        assert {"derived/quality.json", "derived/gaze_a.parquet", "derived/sync.json"} <= set(names)
        assert not [n for n in names if "/." in n or n.endswith(".tmp")]
        assert json.loads(z.read("meta.json")) == json.loads((here.raw / "meta.json").read_text())
        rows = json.loads(z.read("chunks.json"))
    assert [(r["stream"], r["seq"], r["size"], r["sha256"]) for r in rows] == sorted(
        (r.stream, r.seq, r.size, r.sha256) for r in here.manifest
    )
    assert summary.contains_raw_video and summary.files == len(names)


def test_an_export_without_raw_video_has_no_webcam_files(
    here: FixtureSession, migrated_engine: Engine, tmp_path: Path
) -> None:
    summary = export(migrated_engine, here, tmp_path / "s.zip", raw_video=False)
    with zipfile.ZipFile(tmp_path / "s.zip") as z:
        names = z.namelist()
        assert not [n for n in names if "webcam" in n and "webcam_frames" not in n]
        assert "derived/webcam_frames.parquet" in names and "raw/chunks/events/000000.bin" in names
        assert len(json.loads(z.read("chunks.json"))) == len(here.manifest)  # the table is whole
    assert not summary.contains_raw_video


def test_export_of_an_unknown_session_fails(migrated_engine: Engine, tmp_path: Path) -> None:
    with Db(migrated_engine) as db, pytest.raises(ArchiveError, match="does not exist"):
        export_session(db, tmp_path, uuid.UUID(int=5), tmp_path / "x.zip")


def test_a_round_trip_gives_the_same_files_and_rows(
    here: FixtureSession, migrated_engine: Engine, tmp_path: Path
) -> None:
    before_files = files_of(session_directory(here))
    quality = json.loads((session_directory(here) / "derived" / "quality.json").read_text())
    export(migrated_engine, here, tmp_path / "s.zip")
    forget(migrated_engine, here)

    new_root = tmp_path / "other"
    with Db(migrated_engine) as db:
        sid = import_session(db, new_root, tmp_path / "s.zip")
        db.commit()

    assert sid == here.session_id
    imported = new_root / "studies" / str(here.study_id) / "sessions" / str(sid)
    assert files_of(imported) == before_files
    with Db(migrated_engine) as db:
        row = db.get_one(SessionRow, sid)
        assert row.status is SessionStatus.PROCESSED and row.quality == quality
        assert row.participant_code == "AB0315" and row.flags == ["calibration_gate_failed"]
        assert row.raw_video_deleted_at is None
        chunks = db.scalars(select(Chunk).where(Chunk.session_id == sid)).all()
        assert sorted((c.stream.value, c.seq, c.size, c.sha256) for c in chunks) == sorted(
            (r.stream, r.seq, r.size, r.sha256) for r in here.manifest
        )
        study = db.get_one(Study, here.study_id)
        assert study.code == "imported-1" and study.mode is StudyMode.REMOTE
        # The sequence moved past the imported id: the next study does not collide with it.
        later = Study(name="next", code="next", mode=StudyMode.REMOTE, config={})
        db.add(later)
        db.flush()
        assert later.id > here.study_id
        # The upload token of an imported session cannot be guessed or used.
        assert len(row.upload_token_hash) == 64


def test_an_imported_session_can_be_processed_again(
    here: FixtureSession, migrated_engine: Engine, tmp_path: Path, face_model: Path
) -> None:
    import pyarrow.parquet as pq

    before = pq.read_table(session_directory(here) / "derived" / "gaze_a.parquet").to_pandas()
    export(migrated_engine, here, tmp_path / "s.zip")
    forget(migrated_engine, here)
    new_root = tmp_path / "other"
    with Db(migrated_engine) as db:
        import_session(db, new_root, tmp_path / "s.zip")
        db.commit()
        manifest = [
            ChunkRecord(c.stream.value, c.seq, c.size, c.sha256) for c in db.scalars(select(Chunk))
        ]
    run_pipeline(
        Context(
            new_root, here.study_id, here.session_id, CONFIG, face_model, 32 * 1024 * 1024, manifest
        )
    )
    after = pq.read_table(
        new_root
        / "studies"
        / "1"
        / "sessions"
        / str(here.session_id)
        / "derived"
        / "gaze_a.parquet"
    ).to_pandas()
    assert after.equals(before)


def test_a_session_that_exists_is_not_replaced_without_force(
    here: FixtureSession, migrated_engine: Engine, tmp_path: Path
) -> None:
    export(migrated_engine, here, tmp_path / "s.zip")
    with Db(migrated_engine) as db, pytest.raises(ArchiveError, match="already exists"):
        import_session(db, here.root, tmp_path / "s.zip")
    marker = session_directory(here) / "derived" / "marker.txt"
    marker.write_text("local change")
    with Db(migrated_engine) as db:
        import_session(db, here.root, tmp_path / "s.zip", force=True)
        db.commit()
    assert not marker.exists()  # replaced by the archive's files
    with Db(migrated_engine) as db:
        assert len(db.scalars(select(Chunk)).all()) == len(here.manifest)


def test_an_export_without_raw_video_imports_with_the_video_marked_deleted(
    here: FixtureSession, migrated_engine: Engine, tmp_path: Path
) -> None:
    export(migrated_engine, here, tmp_path / "s.zip", raw_video=False)
    forget(migrated_engine, here)
    with Db(migrated_engine) as db:
        sid = import_session(db, tmp_path / "other", tmp_path / "s.zip")
        db.commit()
        row = db.get_one(SessionRow, sid)
        assert row.raw_video_deleted_at is not None
        streams = {c.stream.value for c in db.scalars(select(Chunk))}
        assert "webcam" not in streams and "events" in streams


def write_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def test_unsafe_or_foreign_archives_are_refused(migrated_engine: Engine, tmp_path: Path) -> None:
    meta = json.dumps(
        {"id": str(uuid.UUID(int=1)), "study_id": 1, "participant_code": "AB0315", "config": {}}
    )
    good = {"meta.json": meta.encode(), "chunks.json": b"[]"}
    cases = {
        "../escape.txt": "unsafe path",
        "/abs.txt": "unsafe path",
        "raw/../../escape.txt": "unsafe path",
        "other/file.txt": "unexpected file",
    }
    with Db(migrated_engine) as db:
        for name, message in cases.items():
            archive = write_zip(tmp_path / "bad.zip", {**good, name: b"x"})
            with pytest.raises(ArchiveError, match=message):
                import_session(db, tmp_path / "root", archive)
        with pytest.raises(ArchiveError, match="no chunks.json"):
            import_session(
                db, tmp_path / "root", write_zip(tmp_path / "b.zip", {"meta.json": b"{}"})
            )
        (tmp_path / "notzip.zip").write_bytes(b"this is not a zip")
        with pytest.raises(ArchiveError, match="not a readable ZIP"):
            import_session(db, tmp_path / "root", tmp_path / "notzip.zip")
        with pytest.raises(ArchiveError, match="not a session's"):
            import_session(
                db, tmp_path / "root", write_zip(tmp_path / "c.zip", {**good, "meta.json": b"{}"})
            )
    assert not (tmp_path / "root").exists()


def test_a_changed_chunk_is_refused_and_leaves_nothing_behind(
    here: FixtureSession, migrated_engine: Engine, tmp_path: Path
) -> None:
    export(migrated_engine, here, tmp_path / "s.zip")
    forget(migrated_engine, here)
    with zipfile.ZipFile(tmp_path / "s.zip") as z:
        files = {n: z.read(n) for n in z.namelist()}
    data = bytearray(files["raw/chunks/events/000001.bin"])
    data[5] ^= 0xFF
    files["raw/chunks/events/000001.bin"] = bytes(data)
    write_zip(tmp_path / "bad.zip", files)
    with (
        Db(migrated_engine) as db,
        pytest.raises(ArchiveError, match="events chunk 1 does not match"),
    ):
        import_session(db, tmp_path / "other", tmp_path / "bad.zip")
    with Db(migrated_engine) as db:
        assert db.scalars(select(SessionRow)).all() == [] and db.scalars(select(Chunk)).all() == []
    assert not (tmp_path / "other" / "studies" / "1" / "sessions" / str(here.session_id)).exists()


def test_a_corrupt_archive_with_force_leaves_the_existing_session_untouched(
    here: FixtureSession, migrated_engine: Engine, tmp_path: Path
) -> None:
    export(migrated_engine, here, tmp_path / "s.zip")
    with zipfile.ZipFile(tmp_path / "s.zip") as z:
        files = {n: z.read(n) for n in z.namelist()}
    data = bytearray(files["raw/chunks/events/000001.bin"])
    data[5] ^= 0xFF
    files["raw/chunks/events/000001.bin"] = bytes(data)
    write_zip(tmp_path / "bad.zip", files)

    before = files_of(session_directory(here))
    with Db(migrated_engine) as db, pytest.raises(ArchiveError, match="does not match"):
        import_session(db, here.root, tmp_path / "bad.zip", force=True)

    assert files_of(session_directory(here)) == before  # every raw and derived file still there
    sessions_dir = session_directory(here).parent
    assert [p.name for p in sessions_dir.iterdir()] == [str(here.session_id)]  # no staging left
    with Db(migrated_engine) as db:
        assert db.get_one(SessionRow, here.session_id).status is SessionStatus.PROCESSED
        assert len(db.scalars(select(Chunk)).all()) == len(here.manifest)


def test_a_failure_while_adding_the_rows_puts_the_old_session_back(
    here: FixtureSession, migrated_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.session_archive as archive_module

    export(migrated_engine, here, tmp_path / "s.zip")
    before = files_of(session_directory(here))

    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(archive_module, "_add_rows", broken)
    with Db(migrated_engine) as db, pytest.raises(RuntimeError, match="went away"):
        import_session(db, here.root, tmp_path / "s.zip", force=True)
    assert files_of(session_directory(here)) == before
    assert [p.name for p in session_directory(here).parent.iterdir()] == [str(here.session_id)]


def test_a_session_with_pipeline_b_comes_back_with_b_s_line_in_its_record(
    b_session: FixtureSession, migrated_engine: Engine, tmp_path: Path
) -> None:
    """Pipeline B can be run on another machine and the session sent back (an export carries all of
    `derived/`, B's files included): the import puts B's line into the record again."""
    from app.pipeline_b import summary_of

    s = replace(b_session)
    seed_session(migrated_engine, s, status=SessionStatus.PROCESSED)
    derived = session_directory(s) / "derived"
    expected = summary_of(derived)
    assert expected is not None and expected["model"] == "l2cs_mobileone_s0"
    export(migrated_engine, s, tmp_path / "b.zip")
    forget(migrated_engine, s)
    with Db(migrated_engine) as db:
        sid = import_session(db, tmp_path / "other", tmp_path / "b.zip")
        db.commit()
    with Db(migrated_engine) as db:
        row = db.get_one(SessionRow, sid)
        assert row.quality is not None and row.quality["pipeline_b"] == expected
        assert row.quality["inclusion"]  # A's report is there too
