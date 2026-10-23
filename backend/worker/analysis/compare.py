"""Pipeline A against Pipeline B on the same recordings (PLAN §9.5; DECISIONS 2026-10-20).

For every session of a study that has both pipelines fresh (B not stale) and a successful
calibration in both, regardless of A's inclusion decision (it would bias the comparison), the
validation accuracy and precision and the valid fraction of each pipeline are put side by side;
the paired Wilcoxon signed-rank test says whether the difference is systematic. The outputs are a
per-session CSV, a statistics CSV, a short Markdown report and box plots, in
`analysis/{study_id}/compare/{run_id}/` with a `run.json` like the analysis runs'.

Lower is better for the errors and the precision (RMS of the step between samples), higher for the
valid fraction. The test is two-sided, per metric, with no correction for several metrics. Below
`compare_min_pairs` sessions the report gives the numbers and no p-value.
"""

import csv
import io
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
from scipy import stats
from sqlalchemy import select
from sqlalchemy.orm import Session as Db

from app.analysis_runs import RUN_FILE
from app.models import Session as SessionRow
from app.models import SessionStatus
from app.pipeline_b import QUALITY_B_FILE
from app.storage import atomic_write_bytes, compare_run_dir, derived_dir
from worker.analysis.config import AnalysisConfig, git_hash
from worker.pipeline.quality import QUALITY_FILE

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

log = logging.getLogger("worker")

COMPARISON_FILE = "comparison.csv"
STATISTICS_FILE = "statistics.csv"
REPORT_FILE = "report.md"
BOXPLOTS_FILE = "boxplots.png"
COMPARE_VERSION = 1
DEBUG_SESSION = "debug_session"
CALIBRATION_FAILED_A = "calibration_failed_a"
CALIBRATION_FAILED_B = "calibration_failed_b"
PIPELINE_B_NOT_RUN = "pipeline_b_not_run"
PIPELINE_B_STALE = "pipeline_b_stale"
NOT_PROCESSED = "not_processed"
QUALITY_FILE_MISSING = "quality_file_missing"


@dataclass(frozen=True)
class Metric:
    name: str
    label: str
    lower_is_better: bool


METRICS = (
    Metric("accuracy_norm", "validation error (share of the viewport diagonal)", True),
    Metric("accuracy_corrected_norm", "validation error after drift correction (share)", True),
    Metric("accuracy_px", "validation error (px)", True),
    Metric("precision_px", "precision, RMS step between samples (px)", True),
    Metric("valid_fraction", "valid gaze during playback (share)", False),
)


def numbers_of(report: dict[str, Any]) -> dict[str, float | None]:
    """The compared numbers of a quality report (A's or B's: the same keys)."""
    validation = report.get("validation") or {}
    playback = report.get("playback") or {}
    blocks = validation.get("blocks") or []
    px = [b["mean_error_px"] for b in blocks if b.get("mean_error_px") is not None]
    return {
        "accuracy_norm": validation.get("mean_error_norm"),
        "accuracy_corrected_norm": validation.get("mean_error_corrected_norm"),
        "accuracy_px": float(np.mean(px)) if px else None,
        "precision_px": validation.get("precision_px"),
        "valid_fraction": playback.get("valid_fraction"),
    }


@dataclass(frozen=True)
class SessionNumbers:
    session_id: str
    participant_code: str
    a: dict[str, float | None]
    b: dict[str, float | None]


@dataclass(frozen=True)
class Excluded:
    session_id: str
    participant_code: str
    reasons: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "participant_code": self.participant_code,
            "reasons": self.reasons,
        }


def collect(db: Db, root: Path, study_id: int) -> tuple[list[SessionNumbers], list[Excluded]]:
    """The sessions of the study that can be compared, with both pipelines' numbers, and the ones
    that cannot, with the reasons."""
    rows = db.scalars(
        select(SessionRow)
        .where(
            SessionRow.study_id == study_id,
            SessionRow.status.in_(
                (SessionStatus.UPLOADED, SessionStatus.PROCESSING, SessionStatus.PROCESSED,
                 SessionStatus.FAILED)
            ),
        )
        .order_by(SessionRow.participant_code, SessionRow.created_at)
    ).all()  # fmt: skip
    used: list[SessionNumbers] = []
    left: list[Excluded] = []
    for row in rows:
        reasons = _problems(row)
        numbers: tuple[dict[str, float | None], dict[str, float | None]] | None = None
        if not reasons:
            derived = derived_dir(root, study_id, row.id)
            try:
                a = json.loads((derived / QUALITY_FILE).read_text(encoding="utf-8"))
                b = json.loads((derived / QUALITY_B_FILE).read_text(encoding="utf-8"))
                numbers = (numbers_of(a), numbers_of(b))
            except (OSError, ValueError):
                reasons = [QUALITY_FILE_MISSING]
        if numbers is None:
            left.append(Excluded(str(row.id), row.participant_code, reasons))
        else:
            used.append(SessionNumbers(str(row.id), row.participant_code, *numbers))
    return used, left


def _problems(row: SessionRow) -> list[str]:
    if row.status is not SessionStatus.PROCESSED or row.quality is None:
        return [NOT_PROCESSED]
    if row.is_debug:
        return [DEBUG_SESSION]
    if (row.quality.get("calibration") or {}).get("status") != "ok":
        return [CALIBRATION_FAILED_A]
    b = row.quality.get("pipeline_b")
    if b is None:
        return [PIPELINE_B_NOT_RUN]
    if b.get("stale"):
        return [PIPELINE_B_STALE]
    if b.get("calibration_status") != "ok":
        return [CALIBRATION_FAILED_B]
    return []


@dataclass(frozen=True)
class MetricStats:
    metric: str
    label: str
    lower_is_better: bool
    n: int
    median_a: float | None
    median_b: float | None
    median_difference: float | None  # B minus A
    b_better: int  # sessions where B is better than A
    a_better: int
    statistic: float | None
    p_value: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric, "label": self.label, "lower_is_better": self.lower_is_better,
            "n": self.n, "median_a": self.median_a, "median_b": self.median_b,
            "median_difference": self.median_difference, "b_better": self.b_better,
            "a_better": self.a_better, "statistic": self.statistic, "p_value": self.p_value,
        }  # fmt: skip


def metric_stats(sessions: list[SessionNumbers], metric: Metric, min_pairs: int) -> MetricStats:
    """The paired comparison of one metric over the sessions that have it in both pipelines."""
    pairs = [
        (s.a[metric.name], s.b[metric.name])
        for s in sessions
        if s.a[metric.name] is not None and s.b[metric.name] is not None
    ]
    a = np.array([p[0] for p in pairs], dtype=np.float64)
    b = np.array([p[1] for p in pairs], dtype=np.float64)
    n = len(pairs)
    better = (b < a) if metric.lower_is_better else (b > a)
    worse = (b > a) if metric.lower_is_better else (b < a)
    statistic = p_value = None
    if n >= min_pairs and np.any(a != b):  # the test is undefined when every difference is 0
        result = stats.wilcoxon(a, b)
        statistic, p_value = float(result.statistic), float(result.pvalue)
    return MetricStats(
        metric.name, metric.label, metric.lower_is_better, n,
        float(np.median(a)) if n else None, float(np.median(b)) if n else None,
        float(np.median(b - a)) if n else None, int(better.sum()), int(worse.sum()),
        statistic, p_value,
    )  # fmt: skip


def _cell(value: float | None) -> str:
    return "" if value is None or not math.isfinite(value) else repr(round(float(value), 6))


def comparison_csv(sessions: list[SessionNumbers]) -> bytes:
    """One row per session: both pipelines' numbers and B minus A."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    header = ["participant_code", "session_id"]
    for m in METRICS:
        header += [f"{m.name}_a", f"{m.name}_b", f"{m.name}_diff"]
    writer.writerow(header)
    for s in sessions:
        row: list[str] = [s.participant_code, s.session_id]
        for m in METRICS:
            a, b = s.a[m.name], s.b[m.name]
            row += [_cell(a), _cell(b), _cell(None if a is None or b is None else b - a)]
        writer.writerow(row)
    return out.getvalue().encode()


def statistics_csv(all_stats: list[MetricStats]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["metric", "n", "median_a", "median_b", "median_diff_b_minus_a",
                     "b_better", "a_better", "wilcoxon_statistic", "p_value"])  # fmt: skip
    for s in all_stats:
        writer.writerow([s.metric, s.n, _cell(s.median_a), _cell(s.median_b),
                         _cell(s.median_difference), s.b_better, s.a_better,
                         _cell(s.statistic), _cell(s.p_value)])  # fmt: skip
    return out.getvalue().encode()


def boxplots_png(sessions: list[SessionNumbers], all_stats: list[MetricStats]) -> bytes:
    """A box plot of A and B per metric, with a line for every session joining its two values."""
    figure, axes = plt.subplots(1, len(METRICS), figsize=(3.2 * len(METRICS), 4.2), dpi=110)
    for axis, metric, s in zip(axes, METRICS, all_stats, strict=True):
        pairs = [
            (x.a[metric.name], x.b[metric.name]) for x in sessions
            if x.a[metric.name] is not None and x.b[metric.name] is not None
        ]  # fmt: skip
        if pairs:
            a, b = [p[0] for p in pairs], [p[1] for p in pairs]
            axis.boxplot([a, b], tick_labels=["A", "B"], widths=0.5)
            for x, y in pairs:
                axis.plot([1, 2], [x, y], color="#888", linewidth=0.7, marker="o", markersize=3)
        else:
            axis.text(0.5, 0.5, "no data", transform=axis.transAxes, ha="center", color="#666")
        p_text = "p = n/a" if s.p_value is None else f"p = {s.p_value:.3g}"
        axis.set_title(f"{metric.name}\nn = {s.n}, {p_text}", fontsize=8)
        axis.tick_params(labelsize=8)
    figure.suptitle(
        "Pipeline A against Pipeline B (lower is better except valid_fraction)", fontsize=9
    )
    figure.tight_layout()
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png")
    plt.close(figure)
    return buffer.getvalue()


def report_markdown(
    study_id: int,
    sessions: list[SessionNumbers],
    excluded: list[Excluded],
    all_stats: list[MetricStats],
    config: AnalysisConfig,
) -> bytes:
    lines = [
        f"# Pipeline A against Pipeline B, study {study_id}",
        "",
        f"{len(sessions)} session(s) compared, {len(excluded)} left out. "
        "A paired Wilcoxon signed-rank test (two-sided) per metric; differences are B minus A; "
        "lower is better except for the valid fraction. There is no correction for testing "
        f"several metrics, and a p-value is only given from {config.compare_min_pairs} pairs.",
        "",
        "| metric | n | median A | median B | median B - A | B better | A better | p |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in all_stats:
        p = "n/a" if s.p_value is None else f"{s.p_value:.3g}"
        lines.append(
            f"| {s.label} | {s.n} | {_cell(s.median_a) or '-'} | {_cell(s.median_b) or '-'} "
            f"| {_cell(s.median_difference) or '-'} | {s.b_better} | {s.a_better} | {p} |"
        )
    lines += ["", f"![box plots]({BOXPLOTS_FILE})", "", f"Per session: `{COMPARISON_FILE}`.", ""]
    if excluded:
        lines += ["## Left out", ""]
        lines += [f"- {e.participant_code} ({e.session_id[:8]}): {', '.join(e.reasons)}"
                  for e in excluded]  # fmt: skip
        lines.append("")
    return "\n".join(lines).encode()


@dataclass
class CompareRecord:
    run_id: str
    study_id: int
    config: AnalysisConfig
    started_at: str
    git_hash: str | None
    included: list[dict[str, Any]]
    excluded: list[dict[str, Any]]
    status: str = "running"
    error: str | None = None
    finished_at: str | None = None
    outputs: list[str] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": COMPARE_VERSION, "kind": "compare_pipelines", "run_id": self.run_id,
            "study_id": self.study_id, "status": self.status, "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "git_hash": self.git_hash,
            "parameters": {"compare_min_pairs": self.config.compare_min_pairs},
            "included": self.included, "excluded": self.excluded,
            "outputs": self.outputs, "results": self.results,
        }  # fmt: skip

    def write(self, run_dir: Path) -> None:
        text = json.dumps(self.as_dict(), indent=2) + "\n"
        atomic_write_bytes(run_dir / RUN_FILE, text.encode())

    def finish(self, run_dir: Path, error: str | None = None) -> None:
        self.status = "failed" if error else "done"
        self.error = error
        self.finished_at = datetime.now(UTC).isoformat()
        self.write(run_dir)


class CompareError(Exception):
    """The comparison cannot produce anything (a data problem, not a crash)."""


def run_compare(
    db: Db, root: Path, study_id: int, run_id: str, config: AnalysisConfig
) -> CompareRecord:
    """Make the comparison of a study into `analysis/{study_id}/compare/{run_id}/`. A run with no
    comparable session is written as failed and raises `CompareError`."""
    sessions, excluded = collect(db, root, study_id)
    run_dir = compare_run_dir(root, study_id, run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    record = CompareRecord(
        run_id=run_id, study_id=study_id, config=config,
        started_at=datetime.now(UTC).isoformat(), git_hash=git_hash(),
        included=[{"session_id": s.session_id, "participant_code": s.participant_code}
                  for s in sessions],
        excluded=[e.as_dict() for e in excluded],
    )  # fmt: skip
    record.write(run_dir)
    try:
        if not sessions:
            raise CompareError(
                "no session has both pipelines (fresh, with a successful calibration): "
                "see `excluded`"
            )
        all_stats = [metric_stats(sessions, m, config.compare_min_pairs) for m in METRICS]
        atomic_write_bytes(run_dir / COMPARISON_FILE, comparison_csv(sessions))
        atomic_write_bytes(run_dir / STATISTICS_FILE, statistics_csv(all_stats))
        atomic_write_bytes(run_dir / BOXPLOTS_FILE, boxplots_png(sessions, all_stats))
        atomic_write_bytes(
            run_dir / REPORT_FILE, report_markdown(study_id, sessions, excluded, all_stats, config)
        )
        record.outputs = [COMPARISON_FILE, STATISTICS_FILE, REPORT_FILE, BOXPLOTS_FILE]
        record.results = {"sessions": len(sessions), "statistics": [s.as_dict() for s in all_stats]}
    except BaseException as exc:
        record.finish(run_dir, error=str(exc) or type(exc).__name__)
        raise
    record.finish(run_dir)
    return record
