"""Files and ZIP of an analysis run, for the admin only (PLAN §6, §9)."""

import io
import zipfile
from collections.abc import Generator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from tests.adminutil import Admin, anonymous_client, logged_in_admin
from tests.analysis_fake import FILES, make_run


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def admin(migrated_engine: Engine, data_root: Path) -> Generator[Admin]:
    with logged_in_admin(migrated_engine, data_root) as a:
        yield a


def test_the_zip_holds_run_json_and_every_output(admin: Admin, data_root: Path) -> None:
    run_id = make_run(data_root)
    r = admin.client.get(f"/admin/analysis/1/1/{run_id}/download.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert f"analysis_1_1_{run_id}.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as archive:
        assert archive.testzip() is None
        assert sorted(archive.namelist()) == sorted(f"{run_id}/{n}" for n in ["run.json", *FILES])
        for name, data in FILES.items():
            assert archive.read(f"{run_id}/{name}") == data
        info = {i.filename: i.compress_type for i in archive.infolist()}
        assert info[f"{run_id}/heatmap.mp4"] == zipfile.ZIP_STORED
        assert info[f"{run_id}/aoi_metrics_wide.csv"] == zipfile.ZIP_DEFLATED


def test_the_zip_is_for_logged_in_admins_only(migrated_engine: Engine, data_root: Path) -> None:
    run_id = make_run(data_root)
    with anonymous_client(migrated_engine, data_root) as stranger:
        for path in ("download.zip", "files/heatmap.mp4", "files/run.json"):
            assert stranger.get(f"/admin/analysis/1/1/{run_id}/{path}").status_code == 401


def test_a_missing_or_malformed_run_is_404(admin: Admin, data_root: Path) -> None:
    run_id = make_run(data_root)
    c = admin.client
    assert c.get("/admin/analysis/1/1/20260101T000000Z-aaaaaa/download.zip").status_code == 404
    assert c.get(f"/admin/analysis/1/2/{run_id}/download.zip").status_code == 404
    for bad in ("..", "x", "20260101T000000Z-zzzzzz"):
        assert c.get(f"/admin/analysis/1/1/{bad}/download.zip").status_code == 404


def test_a_zip_with_a_missing_file_fails_loudly(admin: Admin, data_root: Path) -> None:
    run_id = make_run(data_root)
    (data_root / "analysis" / "1" / "1" / run_id / "synchrony.png").unlink()
    r = admin.client.get(f"/admin/analysis/1/1/{run_id}/download.zip")
    assert r.status_code == 404 and "synchrony.png" in r.json()["detail"]


def test_only_files_the_run_lists_are_served(admin: Admin, data_root: Path) -> None:
    run_id = make_run(data_root)
    (data_root / "analysis" / "1" / "1" / run_id / "secret.txt").write_text("x")
    c = admin.client
    ok = c.get(f"/admin/analysis/1/1/{run_id}/files/aoi_metrics_wide.csv")
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("text/csv")
    assert c.get(f"/admin/analysis/1/1/{run_id}/files/static/001_window_1.png").status_code == 200
    assert c.get(f"/admin/analysis/1/1/{run_id}/files/run.json").status_code == 200
    for name in ("secret.txt", "../run.json", "..%2Frun.json", "static", "nope.png"):
        assert c.get(f"/admin/analysis/1/1/{run_id}/files/{name}").status_code == 404, name
    part = c.get(f"/admin/analysis/1/1/{run_id}/files/heatmap.mp4", headers={"Range": "bytes=0-9"})
    assert part.status_code == 206 and len(part.content) == 10
