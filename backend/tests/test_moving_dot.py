"""Accuracy against the moving dot (PLAN §12.4): the pilot check of Milestone 3."""

import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pytest
from sqlalchemy import Engine
from typer.testing import CliRunner

import cli
from app.db import make_session_factory
from app.models import SessionStatus
from app.schemas import EventRecord
from tests.dbutil import make_settings
from tests.session_db import seed_session
from tests.session_fixture import FixtureSession
from tests.synthetic_session import (
    VIDEO_FIRST_FRAME_MS,
    VIDEO_RECT,
    Synthetic,
    build,
    truth_at,
)
from worker.pipeline.config import PipelineConfig
from worker.pipeline.gaze import run_gaze
from worker.pipeline.moving_dot import DotPath, MovingDotError, format_report, moving_dot_report

CONFIG = PipelineConfig()
PATH_FILE = Path(__file__).resolve().parents[2] / "e2e" / "fixtures" / "moving_dot_path.json"
PATH = DotPath.load(PATH_FILE)
FPS = 30


def events_for_full_screen_video() -> list[EventRecord]:
    """A 1280 x 720 viewport with the video filling it."""
    size = {"width": 1280.0, "height": 720.0}
    data = {"viewport": size, "screen": size, "device_pixel_ratio": 1,
            "video_rect": {"x": 0.0, "y": 0.0, "width": 1280.0, "height": 720.0}}  # fmt: skip
    return [EventRecord.model_validate({"t": 0, "type": "viewport_change", "data": data})]


def gaze_table(
    offset_px: tuple[float, float] = (0.0, 0.0),
    noise_px: float = 0.0,
    delay_s: float = 0.0,
    valid_share: float = 1.0,
    video_id: str = "1",
    seed: int = 0,
) -> pa.Table:
    """What `gaze_a.parquet` holds when the participant follows the dot (with an offset, a delay
    and noise), as video-normalised coordinates."""
    rng = np.random.default_rng(seed)
    stim = np.arange(0.0, PATH.duration_s - 0.05, 1 / FPS)
    tx, ty = PATH.at(stim - delay_s)
    x = tx + (offset_px[0] + rng.normal(0, noise_px, len(stim))) / 1280
    y = ty + (offset_px[1] + rng.normal(0, noise_px, len(stim))) / 720
    valid = rng.uniform(size=len(stim)) < valid_share
    return pa.table(
        {
            "t": pa.array(1000 + stim * 1000),
            "stim_time": pa.array(stim),
            "x_vid": pa.array(x),
            "y_vid": pa.array(y),
            "x_vid_dc": pa.array(x - offset_px[0] / 1280),
            "y_vid_dc": pa.array(y - offset_px[1] / 720),
            "valid": pa.array(valid),
            "phase": pa.array(["playback"] * len(stim)),
            "video_id": pa.array([video_id] * len(stim)),
        }
    )


def report(table: pa.Table, **kw: Any) -> dict[str, Any]:
    return moving_dot_report(table, events_for_full_screen_video(), PATH, CONFIG, **kw)


def test_following_the_dot_closely_passes() -> None:
    r = report(gaze_table(offset_px=(15.0, -10.0), noise_px=8.0))
    assert r["passes"] and r["reasons"] == []
    assert r["uncorrected"]["median_px"] == pytest.approx(np.hypot(15, 10), abs=4)
    assert r["uncorrected"]["median_norm"] == pytest.approx(
        np.hypot(15, 10) / np.hypot(1280, 720), abs=0.005
    )
    assert r["drift_corrected"]["median_px"] < 10  # the offset taken out leaves the noise
    assert r["valid_fraction"] == 1.0 and r["playback_frames"] > 600


def test_far_off_the_dot_fails_on_the_error() -> None:
    r = report(gaze_table(offset_px=(300.0, 200.0)))
    assert not r["passes"] and r["reasons"] == ["median_error_high"]
    assert r["uncorrected"]["median_norm"] > 0.15
    assert r["drift_corrected"]["median_norm"] < 0.01  # a pure offset: the correction removes it


def test_too_few_valid_frames_fails() -> None:
    r = report(gaze_table(valid_share=0.6))
    assert not r["passes"] and r["reasons"] == ["valid_fraction_low"]
    assert r["valid_fraction"] == pytest.approx(0.6, abs=0.05)


def test_the_thresholds_are_config_values() -> None:
    table = gaze_table(offset_px=(300.0, 200.0), valid_share=0.6)
    lenient = replace(CONFIG, dot_max_median_error_norm=0.5, dot_min_valid_fraction=0.5)
    assert moving_dot_report(table, events_for_full_screen_video(), PATH, lenient)["passes"]


def test_the_gaze_lag_is_found() -> None:
    for delay_ms in (0, 120, -80):
        r = report(gaze_table(delay_s=delay_ms / 1000, noise_px=2.0))
        assert r["gaze_lag_ms"] == pytest.approx(delay_ms, abs=40), delay_ms  # positive: behind


def test_pauses_and_movements_are_reported_apart() -> None:
    table = gaze_table(
        noise_px=1.0, delay_s=0.2
    )  # exact while the dot rests, behind while it moves
    r = report(table)["uncorrected"]
    assert r["pauses"]["frames"] > 0 and r["movements"]["frames"] > 0
    assert r["pauses"]["median_px"] < r["movements"]["median_px"]
    assert r["pauses"]["frames"] + r["movements"]["frames"] == r["frames"]


def test_several_videos_need_an_id_and_no_playback_is_an_error() -> None:
    a, b = gaze_table(video_id="1"), gaze_table(video_id="2")
    both = pa.concat_tables([a, b])
    with pytest.raises(MovingDotError, match="several videos"):
        report(both)
    assert report(both, video_id="2")["video_id"] == "2"
    other = a.set_column(
        a.schema.get_field_index("phase"), "phase", pa.array(["other"] * a.num_rows)
    )
    with pytest.raises(MovingDotError, match="no playback frames"):
        report(other)
    with pytest.raises(MovingDotError, match="inside the path"):
        report(a, video_id="3") if False else report(
            a.set_column(
                a.schema.get_field_index("stim_time"), "stim_time", pa.array([99.0] * a.num_rows)
            )
        )


def test_the_text_report_says_the_verdict() -> None:
    assert format_report(report(gaze_table())).splitlines()[-1] == "PASS"
    failing = format_report(report(gaze_table(offset_px=(400.0, 0.0))))
    assert failing.splitlines()[-1] == "FAIL: median_error_high"
    assert "drift_corrected" in failing and "gaze lag" in failing


# --- the command, on a session with a known scanpath ---


runner = CliRunner()


@pytest.fixture(autouse=True)
def no_leftover_log_handlers() -> Iterator[None]:
    """The CLI points logging at the (captured) stderr of the test; take that away again."""
    yield
    logging.getLogger().handlers.clear()


def path_of_the_synthetic_scanpath(tmp_path: Path, shift_px: float = 0.0) -> Path:
    """A path file that is the designed scanpath of the synthetic session, in video pixels."""
    t = np.arange(0.0, 7.9, 0.04)
    x, y = truth_at(VIDEO_FIRST_FRAME_MS + t * 1000, [])
    vx, vy = x - VIDEO_RECT["x"] + shift_px, y  # a shifted path is a wrong ground truth
    samples = [{"t": float(a), "x": float(b), "y": float(c), "frame": i}
               for i, (a, b, c) in enumerate(zip(t, vx, vy, strict=True))]  # fmt: skip
    file = tmp_path / ("wrong.json" if shift_px else "path.json")
    pauses = [{"start_s": 0.0, "end_s": 0.5, "x": 500, "y": 300}]
    data = {"width": 800, "height": 600, "duration_s": 7.9, "samples": samples, "pauses": pauses}
    file.write_text(json.dumps(data))
    return file


@pytest.fixture
def synthetic_in_db(
    migrated_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Synthetic:
    s = build(tmp_path / "data")
    run_gaze(s.root, s.study_id, s.session_id, CONFIG)
    seed_session(
        migrated_engine,
        FixtureSession(s.root, s.study_id, s.session_id, [], s.root),
        SessionStatus.PROCESSED,
    )
    monkeypatch.setattr(cli, "session_factory", lambda: make_session_factory(migrated_engine))
    monkeypatch.setattr(cli, "open_session", lambda: make_session_factory(migrated_engine)())
    monkeypatch.setattr(cli, "settings", lambda: make_settings(data_root=s.root))
    monkeypatch.setattr(cli, "log_level", lambda: "INFO")
    return s


def test_the_command_passes_a_session_that_followed_the_scanpath(
    synthetic_in_db: Synthetic, tmp_path: Path
) -> None:
    s = synthetic_in_db
    result = runner.invoke(cli.app, ["moving-dot-report", str(s.session_id), "--path",
                                     str(path_of_the_synthetic_scanpath(tmp_path))])  # fmt: skip
    assert result.exit_code == 0, result.output
    assert result.output.splitlines()[-1] == "PASS" and "drift_corrected" in result.output
    as_json = runner.invoke(cli.app, ["moving-dot-report", str(s.session_id), "--json", "--path",
                                      str(path_of_the_synthetic_scanpath(tmp_path))])  # fmt: skip
    data = json.loads(as_json.output)
    # The designed drift is 10 to 30 px: the correction brings the error down to about the noise.
    assert data["drift_corrected"]["median_px"] < data["uncorrected"]["median_px"]
    assert data["uncorrected"]["median_px"] > 12 and data["drift_corrected"]["median_px"] < 8


def test_the_command_exits_2_when_the_gaze_does_not_follow_the_dot(
    synthetic_in_db: Synthetic, tmp_path: Path
) -> None:
    wrong = path_of_the_synthetic_scanpath(tmp_path, shift_px=300.0)
    result = runner.invoke(cli.app, ["moving-dot-report", str(synthetic_in_db.session_id),
                                     "--path", str(wrong)])  # fmt: skip
    assert result.exit_code == 2 and "FAIL: median_error_high" in result.output


def test_the_command_explains_what_is_missing(
    synthetic_in_db: Synthetic, tmp_path: Path, migrated_engine: Engine
) -> None:
    unknown = runner.invoke(cli.app, ["moving-dot-report", "00000000-0000-0000-0000-0000000000aa"])
    assert unknown.exit_code == 1 and "does not exist" in unknown.output
    notanid = runner.invoke(cli.app, ["moving-dot-report", "nope"])
    assert notanid.exit_code == 1 and "not a session id" in notanid.output
    gaze = (
        tmp_path
        / "data"
        / "studies"
        / "1"
        / "sessions"
        / str(synthetic_in_db.session_id)
        / "derived"
        / "gaze_a.parquet"
    )
    gaze.unlink()
    unprocessed = runner.invoke(cli.app, ["moving-dot-report", str(synthetic_in_db.session_id)])
    assert unprocessed.exit_code == 1 and "process the session first" in unprocessed.output


def test_the_default_path_is_the_committed_ground_truth() -> None:
    assert cli._default_dot_path().resolve() == PATH_FILE.resolve()


def test_the_plot_option_draws_the_path_and_the_gaze(
    synthetic_in_db: Synthetic, tmp_path: Path
) -> None:
    out = tmp_path / "dot.png"
    path = path_of_the_synthetic_scanpath(tmp_path)
    result = runner.invoke(cli.app, ["moving-dot-report", str(synthetic_in_db.session_id), "--path",
                                     str(path), "--plot", str(out)])  # fmt: skip
    assert result.exit_code == 0, result.output
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and out.stat().st_size > 5000


def test_the_moving_dot_figure_has_the_dot_and_the_gaze() -> None:
    from worker.pipeline.plots import plot_moving_dot

    table = gaze_table(noise_px=3.0)
    figure = plot_moving_dot(table, PATH, report(table))
    x_panel, y_panel = figure.axes
    assert [line.get_label() for line in x_panel.get_lines()] == ["dot", "gaze"]
    assert y_panel.yaxis_inverted() and y_panel.get_xlabel() == "video time (s)"
    assert "PASS" in str(figure.get_suptitle())
