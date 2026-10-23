"""Pipeline A against B (PLAN §9.5): the numbers, the Wilcoxon statistics, the files."""

import csv
import io
import json
from pathlib import Path

import numpy as np
import pytest
from scipy import stats
from sqlalchemy import Engine
from sqlalchemy.orm import Session as Db

from app.models import SessionStatus
from app.storage import compare_run_dir
from tests.compare_data import Numbers, add_session, add_study, report
from worker.analysis.compare import (
    BOXPLOTS_FILE,
    COMPARISON_FILE,
    METRICS,
    REPORT_FILE,
    STATISTICS_FILE,
    CompareError,
    SessionNumbers,
    collect,
    comparison_csv,
    metric_stats,
    numbers_of,
    report_markdown,
    run_compare,
    statistics_csv,
)
from worker.analysis.config import AnalysisConfig

CONFIG = AnalysisConfig()
ACCURACY = METRICS[0]
VALID = METRICS[4]


def pair(a: float | None, b: float | None, code: str = "P") -> SessionNumbers:
    keys = ("accuracy_norm", "accuracy_corrected_norm", "accuracy_px", "precision_px",
            "valid_fraction")  # fmt: skip
    return SessionNumbers("id", code, {k: a for k in keys}, {k: b for k in keys})


def test_the_numbers_come_from_the_validation_blocks_and_the_playback() -> None:
    n = numbers_of(report(Numbers(0.08, 0.05, 140.0, 20.0, 0.75)))
    assert n == {"accuracy_norm": 0.08, "accuracy_corrected_norm": 0.05, "accuracy_px": 140.0,
                 "precision_px": 20.0, "valid_fraction": 0.75}  # fmt: skip
    none = numbers_of({"validation": None, "playback": None})
    assert set(none.values()) == {None}


A8 = [0.10, 0.12, 0.09, 0.15, 0.11, 0.13, 0.14, 0.08]
B8 = [0.08, 0.10, 0.10, 0.11, 0.09, 0.10, 0.12, 0.07]


def test_the_statistics_are_the_paired_wilcoxon_test_of_scipy() -> None:
    sessions = [pair(a, b, f"P{i}") for i, (a, b) in enumerate(zip(A8, B8, strict=True))]
    s = metric_stats(sessions, ACCURACY, min_pairs=6)
    expected = stats.wilcoxon(A8, B8)
    assert s.n == 8 and s.statistic == pytest.approx(float(expected.statistic))
    assert s.p_value == pytest.approx(float(expected.pvalue))
    assert s.median_a == pytest.approx(float(np.median(A8)))
    assert s.median_b == pytest.approx(float(np.median(B8)))
    assert s.median_difference == pytest.approx(float(np.median(np.array(B8) - np.array(A8))))
    assert (s.b_better, s.a_better) == (7, 1)  # B has the lower error in 7 of 8 sessions


def test_for_the_valid_fraction_higher_is_better() -> None:
    sessions = [pair(0.6 + 0.01 * i, 0.7 + 0.01 * i) for i in range(7)]
    s = metric_stats(sessions, VALID, min_pairs=6)
    assert (s.b_better, s.a_better) == (7, 0) and s.median_difference == pytest.approx(0.1)


def test_below_the_minimum_the_numbers_are_given_and_no_p() -> None:
    s = metric_stats([pair(a, b) for a, b in zip(A8[:5], B8[:5], strict=True)], ACCURACY, 6)
    assert s.n == 5 and s.p_value is None and s.statistic is None
    assert s.median_a is not None and s.median_b is not None
    assert metric_stats([pair(a, b) for a, b in zip(A8[:5], B8[:5], strict=True)], ACCURACY, 5
                        ).p_value is not None  # fmt: skip


def test_no_difference_at_all_has_no_p_value() -> None:
    s = metric_stats([pair(0.1, 0.1, f"P{i}") for i in range(8)], ACCURACY, min_pairs=6)
    assert s.n == 8 and s.p_value is None and s.median_difference == 0.0
    assert (s.b_better, s.a_better) == (0, 0)


def test_a_session_without_a_number_in_one_pipeline_is_left_out_of_that_metric() -> None:
    sessions = [pair(a, b, f"P{i}") for i, (a, b) in enumerate(zip(A8, B8, strict=True))]
    sessions[0] = pair(None, 0.5, "P0")
    sessions[1] = pair(0.5, None, "P1")
    s = metric_stats(sessions, ACCURACY, min_pairs=6)
    assert s.n == 6
    empty = metric_stats([], ACCURACY, 6)
    assert empty.n == 0 and empty.median_a is None and empty.p_value is None


def test_the_csv_files_hold_the_differences_and_the_statistics() -> None:
    sessions = [pair(0.1, 0.07, "AB"), pair(0.2, None, "CD")]
    rows = list(csv.DictReader(io.StringIO(comparison_csv(sessions).decode())))
    assert [r["participant_code"] for r in rows] == ["AB", "CD"]
    assert rows[0]["accuracy_norm_a"] == "0.1" and rows[0]["accuracy_norm_b"] == "0.07"
    assert float(rows[0]["accuracy_norm_diff"]) == pytest.approx(-0.03)
    assert rows[1]["accuracy_norm_b"] == "" and rows[1]["accuracy_norm_diff"] == ""
    all_stats = [metric_stats(sessions, m, 6) for m in METRICS]
    table = list(csv.DictReader(io.StringIO(statistics_csv(all_stats).decode())))
    assert [r["metric"] for r in table] == [m.name for m in METRICS]
    assert table[0]["n"] == "1" and table[0]["p_value"] == ""


def test_the_report_names_the_numbers_the_test_and_who_was_left_out() -> None:
    from worker.analysis.compare import Excluded

    sessions = [pair(a, b, f"P{i}") for i, (a, b) in enumerate(zip(A8, B8, strict=True))]
    all_stats = [metric_stats(sessions, m, 6) for m in METRICS]
    text = report_markdown(
        3, sessions, [Excluded("abcdef12-0000", "ZZ", ["pipeline_b_stale"])], all_stats, CONFIG
    ).decode()
    assert text.startswith("# Pipeline A against Pipeline B, study 3")
    assert "8 session(s) compared, 1 left out" in text and "Wilcoxon" in text
    assert "no correction" in text and f"![box plots]({BOXPLOTS_FILE})" in text
    assert "ZZ (abcdef12): pipeline_b_stale" in text
    p = metric_stats(sessions, ACCURACY, 6).p_value
    assert p is not None and f"{p:.3g}" in text


def test_which_sessions_are_compared_and_why_the_others_are_not(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    add_study(migrated_engine)
    n = Numbers(0.1, 0.08, 150.0, 20.0, 0.9)
    ids = {
        "ok": add_session(migrated_engine, tmp_path, "OK", n, n),
        "a_not_included": add_session(migrated_engine, tmp_path, "AX", n, n),  # A's decision: no
        "uploaded": add_session(
            migrated_engine, tmp_path, "UP", n, n, status=SessionStatus.UPLOADED
        ),
        "debug": add_session(migrated_engine, tmp_path, "DB", n, n, is_debug=True),
        "cal_a": add_session(migrated_engine, tmp_path, "CA", n, n, calibration_a="failed"),
        "no_b": add_session(migrated_engine, tmp_path, "NB", n, None),
        "stale": add_session(migrated_engine, tmp_path, "ST", n, n, stale=True),
        "cal_b": add_session(migrated_engine, tmp_path, "CB", n, n, calibration_b="failed"),
        "no_files": add_session(migrated_engine, tmp_path, "NF", n, n, write_files=False),
    }
    with Db(migrated_engine) as db:
        used, left = collect(db, tmp_path, 1)
    assert sorted(s.participant_code for s in used) == ["AX", "OK"]
    reasons = {e.participant_code: e.reasons for e in left}
    assert reasons == {
        "UP": ["not_processed"], "DB": ["debug_session"], "CA": ["calibration_failed_a"],
        "NB": ["pipeline_b_not_run"], "ST": ["pipeline_b_stale"],
        "CB": ["calibration_failed_b"], "NF": ["quality_file_missing"],
    }  # fmt: skip
    assert len(ids) == 9


def test_a_run_writes_every_file_and_run_json(migrated_engine: Engine, tmp_path: Path) -> None:
    add_study(migrated_engine)
    for i, (a, b) in enumerate(zip(A8, B8, strict=True)):
        a_numbers = Numbers(a, a * 0.8, a * 1000, 20.0 + i, 0.7)
        b_numbers = Numbers(b, b * 0.8, b * 1000, 25.0 + i, 0.8)
        add_session(migrated_engine, tmp_path, f"P{i}", a_numbers, b_numbers)
    add_session(migrated_engine, tmp_path, "OUT", Numbers(0.1, 0.1, 1.0, 1.0, 0.5), None)
    with Db(migrated_engine) as db:
        record = run_compare(db, tmp_path, 1, "20261021T100000Z-abcdef", CONFIG)
    run_dir = compare_run_dir(tmp_path, 1, "20261021T100000Z-abcdef")
    assert record.status == "done"
    for name in (COMPARISON_FILE, STATISTICS_FILE, REPORT_FILE, BOXPLOTS_FILE, "run.json"):
        assert (run_dir / name).stat().st_size > 0, name
    assert (run_dir / BOXPLOTS_FILE).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    data = json.loads((run_dir / "run.json").read_text())
    assert data["kind"] == "compare_pipelines" and data["status"] == "done"
    assert data["parameters"] == {"compare_min_pairs": 6}
    assert len(data["included"]) == 8 and data["excluded"][0]["reasons"] == ["pipeline_b_not_run"]
    assert set(data["outputs"]) == {COMPARISON_FILE, STATISTICS_FILE, REPORT_FILE, BOXPLOTS_FILE}
    accuracy = data["results"]["statistics"][0]
    assert accuracy["p_value"] == pytest.approx(float(stats.wilcoxon(A8, B8).pvalue))
    assert not list(run_dir.glob("*.tmp"))


def test_a_comparison_with_nothing_to_compare_fails_and_says_why(
    migrated_engine: Engine, tmp_path: Path
) -> None:
    add_study(migrated_engine)
    add_session(migrated_engine, tmp_path, "NB", Numbers(0.1, 0.1, 1.0, 1.0, 0.5), None)
    with Db(migrated_engine) as db, pytest.raises(CompareError, match="no session has both"):
        run_compare(db, tmp_path, 1, "20261021T100000Z-abcdef", CONFIG)
    data = json.loads(
        (compare_run_dir(tmp_path, 1, "20261021T100000Z-abcdef") / "run.json").read_text()
    )
    assert data["status"] == "failed" and "no session has both" in data["error"]
    assert data["excluded"][0]["reasons"] == ["pipeline_b_not_run"] and data["outputs"] == []
