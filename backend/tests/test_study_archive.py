"""Export and import of a whole study (PLAN §12.3), on the recorded session copied as a study."""

import json
import shutil
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as Db

from app.models import Chunk, SessionStatus, Study, StudyMode, Video
from app.models import Session as SessionRow
from app.session_archive import ArchiveError
from app.storage import video_aois_path, video_path
from app.study_archive import STUDY_FILE, StudyArchiveSummary, export_study, import_study
from tests.session_fixture import FixtureSession
from tests.study_data import (
    AOIS,
    StudyOnDisk,
    build_study,
    files_of,
    forget_everything,
    session_files,
)


@pytest.fixture
def study(migrated_engine: Engine, a_session: FixtureSession, tmp_path: Path) -> StudyOnDisk:
    return build_study(migrated_engine, a_session, tmp_path / "data")


def export(engine: Engine, s: StudyOnDisk, out: Path, **options: bool) -> StudyArchiveSummary:
    with Db(engine) as db:
        return export_study(db, s.root, s.study_id, out, **options)


def names_of(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        return z.namelist()


def test_the_archive_holds_the_study_its_video_its_sessions_and_its_analyses(
    migrated_engine: Engine, study: StudyOnDisk, tmp_path: Path
) -> None:
    summary = export(migrated_engine, study, tmp_path / "s.zip")
    names = names_of(tmp_path / "s.zip")
    assert STUDY_FILE in names and "videos/1/video.mp4" in names
    sessions = sorted(n for n in names if n.startswith("sessions/"))
    assert sessions == sorted(
        f"sessions/{study.participants[c]}.zip" for c in ("P1", "P2")
    )  # the debug session is left out
    assert any(n.startswith("analysis/1/") and n.endswith("run.json") for n in names)
    assert (summary.sessions, summary.videos, summary.left_out_debug) == (2, 1, 1)
    assert summary.analysis_files > 0
    with zipfile.ZipFile(tmp_path / "s.zip") as z:
        document = json.loads(z.read(STUDY_FILE))
        assert (document["name"], document["code"], document["mode"]) == (
            "Spring study",
            "spring",
            "remote",
        )
        assert document["videos"][0]["aois"] == AOIS and document["config"]["playlist"] == ["1"]
        assert document["contains_raw_video"] is True


def test_the_options_leave_things_out_or_put_debug_sessions_in(
    migrated_engine: Engine, study: StudyOnDisk, tmp_path: Path
) -> None:
    export(migrated_engine, study, tmp_path / "debug.zip", include_debug=True)
    assert len([n for n in names_of(tmp_path / "debug.zip") if n.startswith("sessions/")]) == 3

    export(migrated_engine, study, tmp_path / "plain.zip", include_analysis=False)
    assert not [n for n in names_of(tmp_path / "plain.zip") if n.startswith("analysis/")]

    export(migrated_engine, study, tmp_path / "novideo.zip", include_raw_video=False)
    with zipfile.ZipFile(tmp_path / "novideo.zip") as outer:
        member = f"sessions/{study.participants['P1']}.zip"
        inner = tmp_path / "inner.zip"
        inner.write_bytes(outer.read(member))
    assert not [n for n in names_of(inner) if n == "raw/webcam.webm" or "chunks/webcam" in n]
    assert (
        json.loads(zipfile.ZipFile(tmp_path / "novideo.zip").read(STUDY_FILE))["contains_raw_video"]
        is False
    )


def test_sessions_without_uploaded_data_are_counted_not_exported(
    migrated_engine: Engine, study: StudyOnDisk, tmp_path: Path
) -> None:
    with Db(migrated_engine) as db:
        db.get_one(SessionRow, study.participants["P2"]).status = SessionStatus.CREATED
        db.commit()
    summary = export(migrated_engine, study, tmp_path / "s.zip")
    assert (summary.sessions, summary.left_out_no_data) == (1, 1)


def test_an_unknown_study_or_a_missing_video_file_is_an_error(
    migrated_engine: Engine, study: StudyOnDisk, tmp_path: Path
) -> None:
    with Db(migrated_engine) as db, pytest.raises(ArchiveError, match="does not exist"):
        export_study(db, study.root, 99, tmp_path / "x.zip")
    video_path(study.root, 1).unlink()
    with Db(migrated_engine) as db, pytest.raises(ArchiveError, match="stimulus video 1"):
        export_study(db, study.root, study.study_id, tmp_path / "y.zip")
    assert not (tmp_path / "y.zip").exists()  # nothing half-written is left


def test_a_round_trip_gives_the_same_study_files_and_rows(
    migrated_engine: Engine, study: StudyOnDisk, tmp_path: Path
) -> None:
    before = {c: session_files(study.root, study.study_id, sid)
              for c, sid in study.participants.items() if c != "DBG"}  # fmt: skip
    video_before = files_of(study.root / "videos")
    analysis_before = files_of(study.root / "analysis")
    export(migrated_engine, study, tmp_path / "s.zip")
    forget_everything(migrated_engine)

    other = tmp_path / "other"
    with Db(migrated_engine) as db:
        study_id = import_study(db, other, tmp_path / "s.zip")
        db.commit()

    assert study_id == study.study_id
    for code, files in before.items():
        assert session_files(other, study_id, study.participants[code]) == files, code
    assert not (other / "studies" / "1" / "sessions" / str(study.participants["DBG"])).exists()
    imported_videos = files_of(other / "videos")
    assert imported_videos.pop("1/aois.json")  # the mirror of the AOIs is written (PLAN §7)
    assert imported_videos == video_before
    assert files_of(other / "analysis") == analysis_before
    assert json.loads(video_aois_path(other, 1).read_text()) == AOIS
    with Db(migrated_engine) as db:
        row = db.get_one(Study, study_id)
        assert (row.name, row.code, row.mode) == ("Spring study", "spring", StudyMode.REMOTE)
        assert row.config["playlist"] == ["1"]
        video = db.get_one(Video, 1)
        assert video.sha256 == study.video_sha256 and video.aois == AOIS
        assert (
            video.title == "Dot" and video.width == 1280 and video.file_path == "videos/1/video.mp4"
        )
        rows = db.scalars(select(SessionRow)).all()
        assert sorted(r.participant_code for r in rows) == ["P1", "P2"]
        assert all(r.status is SessionStatus.PROCESSED and r.quality for r in rows)
        assert db.scalars(select(Chunk)).all()
        # the sequences moved past the imported ids: the next study and video do not collide
        later = Study(name="next", code="next", mode=StudyMode.REMOTE, config={})
        db.add(later)
        db.flush()
        assert later.id > study_id
        newer = Video(title="n", file_path="", duration=1, width=1, height=1, fps=1, sha256="x")
        db.add(newer)
        db.flush()
        assert newer.id > 1


def test_what_is_already_there_is_refused_and_nothing_is_left_behind(
    migrated_engine: Engine, study: StudyOnDisk, tmp_path: Path
) -> None:
    export(migrated_engine, study, tmp_path / "s.zip")
    other = tmp_path / "other"
    with Db(migrated_engine) as db, pytest.raises(ArchiveError, match="study with id 1 is already"):
        import_study(db, other, tmp_path / "s.zip")
    assert not other.exists()

    with Db(migrated_engine) as db:  # same code, another id
        db.get_one(Study, 1).code = "spring"
        db.commit()
    forget_everything_but_code(migrated_engine)
    with Db(migrated_engine) as db, pytest.raises(ArchiveError, match="code 'spring' is already"):
        import_study(db, other, tmp_path / "s.zip")
    assert not other.exists()


def forget_everything_but_code(engine: Engine) -> None:
    """Free the id of study 1 but keep another study with its code."""
    with Db(engine) as db:
        forget = Study(id=50, name="other", code="spring", mode=StudyMode.REMOTE, config={})
        for table in (Chunk, SessionRow, Video, Study):
            db.query(table).delete()
        db.add(forget)
        db.commit()


def test_a_video_that_is_there_with_another_file_is_refused_the_same_one_is_reused(
    migrated_engine: Engine, study: StudyOnDisk, tmp_path: Path
) -> None:
    export(migrated_engine, study, tmp_path / "s.zip")
    with Db(migrated_engine) as db:  # forget the study and sessions, keep the video row
        for table in (Chunk, SessionRow, Study):
            db.query(table).delete()
        db.commit()
    other = tmp_path / "other"
    shutil.copytree(study.root / "videos", other / "videos")
    with Db(migrated_engine) as db:  # the same video (same sha256) is used as it is
        assert import_study(db, other, tmp_path / "s.zip") == 1
        db.commit()
    with Db(migrated_engine) as db:
        for table in (Chunk, SessionRow, Study):
            db.query(table).delete()
        db.get_one(Video, 1).sha256 = "0" * 64  # now it is another file
        db.commit()
    third = tmp_path / "third"
    with (
        Db(migrated_engine) as db,
        pytest.raises(ArchiveError, match="video 1 is here with another"),
    ):
        import_study(db, third, tmp_path / "s.zip")
        db.rollback()
    assert not (third / "studies").exists()


def test_a_damaged_session_inside_the_archive_leaves_no_file_and_no_row(
    migrated_engine: Engine, study: StudyOnDisk, tmp_path: Path
) -> None:
    export(migrated_engine, study, tmp_path / "s.zip")
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(tmp_path / "s.zip") as src, zipfile.ZipFile(broken, "w") as dst:
        last = sorted(n for n in src.namelist() if n.startswith("sessions/"))[-1]
        for name in src.namelist():
            data = src.read(name)
            if name == last:
                data = data[:-200] + b"x" * 200  # the nested archive is cut and altered
            dst.writestr(name, data)
    forget_everything(migrated_engine)
    other = tmp_path / "other"
    with Db(migrated_engine) as db:
        with pytest.raises(ArchiveError):
            import_study(db, other, broken)
        db.rollback()
    assert not any(p.is_file() for p in other.rglob("*"))  # no video, session or analysis file
    with Db(migrated_engine) as db:
        assert db.scalars(select(Study)).all() == [] and db.scalars(select(SessionRow)).all() == []


def test_foreign_or_unsafe_archives_are_refused(migrated_engine: Engine, tmp_path: Path) -> None:
    junk = tmp_path / "junk.zip"
    junk.write_bytes(b"not a zip")
    cases = {
        "readable ZIP": junk,
    }
    for name, content, message in (
        ("session.zip", {"meta.json": b"{}", "chunks.json": b"[]"}, "unexpected file"),
        ("empty.zip", {"videos/1/x": b"x"}, "has no study.json"),
        ("unsafe.zip", {"../x": b"x"}, "unsafe path"),
        ("format.zip", {STUDY_FILE: b'{"format": 9, "id": 1}'}, "unknown study archive format"),
    ):
        path = tmp_path / name
        with zipfile.ZipFile(path, "w") as z:
            for member, data in content.items():
                z.writestr(member, data)
        cases[message] = path
    for message, path in cases.items():
        with Db(migrated_engine) as db, pytest.raises(ArchiveError, match=message):
            import_study(db, tmp_path / "root", path)
    assert not (tmp_path / "root").exists()


def with_study_json(source: Path, target: Path, change: dict[str, object]) -> None:
    """A copy of the archive whose study.json has `change` applied (and members renamed by it)."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w") as out:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename == STUDY_FILE:
                data = json.dumps({**json.loads(data), **change}).encode()
            out.writestr(info.filename, data)


def test_a_session_id_that_is_not_a_uuid_cannot_aim_a_write_outside_the_import(
    migrated_engine: Engine, study: StudyOnDisk, tmp_path: Path
) -> None:
    export(migrated_engine, study, tmp_path / "s.zip")
    forget_everything(migrated_engine)
    aimed = (
        tmp_path / "outside" / "evil"
    )  # an absolute id makes `scratch / f"{id}.zip"` leave scratch
    aimed.parent.mkdir()
    planted = tmp_path / "planted.zip"
    with_study_json(tmp_path / "s.zip", planted, {"sessions": [str(aimed)]})
    with zipfile.ZipFile(planted, "a") as z:
        z.writestr(f"sessions/{aimed}.zip", b"bytes that must not be written anywhere else")
    assert f"sessions/{aimed}.zip".startswith("sessions//")  # passes the member-name check
    for hostile in (str(aimed), "not-a-uuid", "../x"):
        changed = tmp_path / "changed.zip"
        with_study_json(planted, changed, {"sessions": [hostile]})
        with Db(migrated_engine) as db, pytest.raises(ArchiveError, match="not a UUID"):
            import_study(db, tmp_path / "root", changed)
    assert list(aimed.parent.iterdir()) == []
    assert not any(p.is_file() for p in (tmp_path / "root").rglob("*"))


def test_a_session_of_another_study_or_another_id_inside_the_archive_is_refused(
    migrated_engine: Engine, study: StudyOnDisk, tmp_path: Path
) -> None:
    export(migrated_engine, study, tmp_path / "s.zip")
    forget_everything(migrated_engine)
    moved = tmp_path / "moved.zip"
    with_study_json(tmp_path / "s.zip", moved, {"id": 5, "code": "other"})  # sessions say study 1
    root = tmp_path / "root"
    with Db(migrated_engine) as db:
        with pytest.raises(ArchiveError, match="of study 1, not session .* of study 5"):
            import_study(db, root, moved)
        db.rollback()
    assert not any(p.is_file() for p in root.rglob("*"))
    assert not list(root.rglob("sessions/*"))  # no directory left behind either
    with Db(migrated_engine) as db:
        assert db.scalars(select(Study)).all() == []


# --- the CLI and the admin button -------------------------------------------------------------


def test_the_cli_exports_and_imports_a_study(
    migrated_engine: Engine, study: StudyOnDisk, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    from typer.testing import CliRunner

    import cli
    from app.db import make_session_factory
    from tests.dbutil import make_settings

    runner = CliRunner()
    settings = make_settings(data_root=study.root)
    monkeypatch.setattr(cli, "settings", lambda: settings)
    monkeypatch.setattr(cli, "open_session", lambda: make_session_factory(migrated_engine)())
    out = tmp_path / "cli.zip"
    result = runner.invoke(cli.app, ["export-study", "1", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert (
        "2 session(s), 1 video(s)" in result.output
        and "left out 1 debug session(s)" in result.output
    )
    assert "personal data" in result.output
    with_debug = runner.invoke(
        cli.app,
        [
            "export-study",
            "1",
            "--out",
            str(tmp_path / "d.zip"),
            "--include-debug",
            "--no-raw-video",
            "--no-analysis",
        ],  # fmt: skip
    )
    assert "3 session(s)" in with_debug.output and "0 analysis file(s)" in with_debug.output
    assert "personal data" not in with_debug.output
    assert runner.invoke(cli.app, ["export-study", "99"]).exit_code == 1

    forget_everything(migrated_engine)
    settings_b = make_settings(data_root=tmp_path / "other")
    monkeypatch.setattr(cli, "settings", lambda: settings_b)
    imported = runner.invoke(cli.app, ["import-study", str(out)])
    assert imported.exit_code == 0 and "Imported study 1" in imported.output
    again = runner.invoke(cli.app, ["import-study", str(out)])
    assert again.exit_code == 1 and "already here" in again.output
    logging.getLogger().handlers.clear()


def test_the_admin_downloads_the_study_and_the_options_apply(
    migrated_engine: Engine, study: StudyOnDisk, tmp_path: Path
) -> None:
    import io

    from tests.adminutil import logged_in_admin

    leftovers = len(list(Path(tempfile_dir()).glob("study-export-*.zip")))
    with logged_in_admin(migrated_engine, study.root) as admin:
        page = admin.client.get("/admin/studies/1").text
        assert (
            "<h2>Export the study</h2>" in page and 'action="/admin/studies/1/export.zip"' in page
        )
        assert page.count('name="raw_video"') == 2  # a hidden "false" and the box
        r = admin.client.get("/admin/studies/1/export.zip")
        assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
        assert "study-spring.zip" in r.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            names = z.namelist()
            assert len([n for n in names if n.startswith("sessions/")]) == 2
        r = admin.client.get(
            "/admin/studies/1/export.zip?debug=true&analysis=false&raw_video=false"
        )
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            names = z.namelist()
            assert len([n for n in names if n.startswith("sessions/")]) == 3
            assert not [n for n in names if n.startswith("analysis/")]
            assert json.loads(z.read(STUDY_FILE))["contains_raw_video"] is False
        assert admin.client.get("/admin/studies/99/export.zip").status_code == 404
    assert len(list(Path(tempfile_dir()).glob("study-export-*.zip"))) == leftovers  # removed again


def test_the_download_needs_a_login(migrated_engine: Engine, study: StudyOnDisk) -> None:
    from tests.adminutil import anonymous_client

    with anonymous_client(migrated_engine, study.root) as stranger:
        assert stranger.get("/admin/studies/1/export.zip").status_code in (303, 401)


def tempfile_dir() -> str:
    import tempfile

    return tempfile.gettempdir()
