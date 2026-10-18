"""The stage status file (PLAN §12.2)."""

from pathlib import Path

from app.stage_status import STAGES_FILE, read_entries, status_of, write_entries


def test_status_of_a_summary() -> None:
    assert status_of({"skipped": "calibration_failed"}) == "skipped"
    assert status_of({"frames": 3}) == "ok" and status_of(None) == "ok"


def test_entries_survive_a_round_trip(tmp_path: Path) -> None:
    entries = {"ingest": {"status": "ok", "seconds": 1.0, "summary": {"a": 1}, "error": None}}
    write_entries(tmp_path, entries)
    assert read_entries(tmp_path) == entries
    assert not [p for p in tmp_path.iterdir() if p.name != STAGES_FILE]  # no temporary file


def test_without_the_file_the_quality_report_is_used() -> None:
    quality = {"stages": {"gaze": {"seconds": 2.0, "summary": {"skipped": "x"}}}}
    entries = read_entries(Path("/nonexistent"), quality)
    assert entries == {
        "gaze": {"status": "skipped", "seconds": 2.0, "summary": {"skipped": "x"}, "error": None}
    }
    assert read_entries(Path("/nonexistent")) == {}
