"""Run an experiment: the product's stages on every session, with the experiment's config, then the
scores against the dot.

Each experiment gets its own data root (`results/<name>/data/`): a session's `raw/` there is a link
to the lab's read-only copy, and `derived/` starts with links to the cached files (landmarks,
webcam frames, features_b). The stages run unchanged on that root: `compute_features`, `run_sync`,
`run_calibration`, `run_gaze` (A, and B where features_b is cached).
"""

import datetime
import json
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from app.events import parse_events
from app.storage import derived_dir, events_path, raw_dir
from lab.experiment import Experiment
from lab.layout import (
    CACHED_FILES,
    REPO_ROOT,
    SessionRef,
    list_sessions,
    reference_dir,
    results_dir,
    sessions_root,
)
from lab.score import DotTruth, Score, format_scores, score_gaze
from lab.variants import Variant, add_highpass, refit_with_validation, rewrite_drift
from worker.pipeline.calibration import run_calibration
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURES_FILE, compute_features
from worker.pipeline.gaze import run_gaze
from worker.pipeline.landmarks import LANDMARKS_FILE
from worker.pipeline.spec import SPECS
from worker.pipeline.sync import run_sync

PRODUCT = Variant()  # the product's feature columns, nothing derived
SCORES_FILE = "scores.json"
REPORT_FILE = "report.txt"


@dataclass(frozen=True)
class Failure:
    study_id: int
    code: str
    pipeline: str
    reason: str


@dataclass(frozen=True)
class RunResult:
    scores: list[Score]
    failures: list[Failure]


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
            check=True, capture_output=True, text=True,
        )  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        return None
    dirty = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=no"],
        capture_output=True, text=True,
    ).stdout.strip()  # fmt: skip
    return out.stdout.strip() + ("-dirty" if dirty else "")


def prepare_session(lab_sessions: Path, work: Path, ref: SessionRef) -> Path:
    """The session's directory under the experiment's data root `work`; returns its derived dir."""
    src_raw = raw_dir(lab_sessions, ref.study_id, ref.session_id)
    raw = raw_dir(work, ref.study_id, ref.session_id)
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.symlink_to(src_raw.resolve(), target_is_directory=True)
    derived = derived_dir(work, ref.study_id, ref.session_id)
    derived.mkdir()
    src_derived = derived_dir(lab_sessions, ref.study_id, ref.session_id)
    for name in CACHED_FILES:
        if (src_derived / name).is_file():
            (derived / name).symlink_to((src_derived / name).resolve())
    return derived


def run_stages(
    work: Path,
    ref: SessionRef,
    config: PipelineConfig,
    pipelines: tuple[str, ...],
    variant: Variant = PRODUCT,
) -> tuple[list[str], list[Failure]]:
    """Features, sync, then calibration and gaze of each pipeline. Returns the pipelines that have
    a gaze table, and why a calibration failed. A pipeline without its features file (B not run on
    the session) is skipped."""
    derived = derived_dir(work, ref.study_id, ref.session_id)
    compute_features(derived / LANDMARKS_FILE, derived / FEATURES_FILE, config)
    run_sync(work, ref.study_id, ref.session_id, config)
    if variant.highpass is not None:
        add_highpass(derived / FEATURES_FILE, variant.highpass)
    done, failures = [], []
    for p in pipelines:
        spec = SPECS[p]
        if p == "a":
            spec = replace(spec, feature_columns=variant.feature_columns)
        if not (derived / spec.features_file).exists():  # B never ran on this session: skip
            continue
        fit = run_calibration(work, ref.study_id, ref.session_id, config, spec)
        if fit.status != "ok":
            failures.append(Failure(ref.study_id, ref.code, p, f"calibration {fit.failed_reason}"))
            continue
        if p == "a" and variant.fit_validation != "none":
            failed = refit_with_validation(work, ref, config, spec, variant.fit_validation)
            if failed is not None:
                failures.append(Failure(ref.study_id, ref.code, p, f"refit {failed}"))
                continue
        run_gaze(work, ref.study_id, ref.session_id, config, spec)
        if p == "a" and variant.drift != "offset":
            rewrite_drift(work, ref, config, spec, variant.drift)
        done.append(p)
    return done, failures


def score_session(
    root: Path,
    gaze_dir: Path,
    ref: SessionRef,
    pipeline: str,
    truth: DotTruth,
    config: PipelineConfig,
) -> Score:
    events = parse_events(events_path(root, ref.study_id, ref.session_id).read_bytes())
    gaze = pq.read_table(gaze_dir / SPECS[pipeline].gaze_file)
    return score_gaze(gaze, events, truth, config, ref.study_id, ref.code, pipeline)


def write_results(out: Path, header: dict[str, Any], result: RunResult, truth: DotTruth) -> str:
    """`scores.json` and `report.txt` in `out`; returns the report."""
    data = {
        **header,
        "scores": [s.as_dict() for s in result.scores],
        "failures": [vars(f) for f in result.failures],
    }
    (out / SCORES_FILE).write_text(json.dumps(data, indent=1) + "\n", encoding="utf-8")
    lines = [f"{k}: {v}" for k, v in header.items() if k != "config"]
    lines += [f"failed: {f.study_id} {f.code} {f.pipeline}: {f.reason}" for f in result.failures]
    report = "\n".join(lines) + "\n\n" + format_scores(result.scores, truth)
    (out / REPORT_FILE).write_text(report, encoding="utf-8")
    return report


def _header(name: str, description: str, overrides: dict[str, Any], config: PipelineConfig,
            studies: dict[str, list[int]]) -> dict[str, Any]:  # fmt: skip
    return {
        "experiment": name,
        "description": description,
        "overrides": overrides,
        **studies,
        "git": _git_sha(),
        "run_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        "config": config.as_dict(),
    }


def variant_summary(v: Variant) -> dict[str, Any]:
    """The variant's departures from the product, for the report header."""
    out: dict[str, Any] = {}
    if v.feature_columns != Variant().feature_columns:
        out["features"] = list(v.feature_columns)
    if v.highpass is not None:
        out["highpass"] = {"columns": list(v.highpass.columns), "window_s": v.highpass.window_s}
    if v.fit_validation != "none":
        out["fit_validation"] = v.fit_validation
    if v.drift != "offset":
        out["drift"] = v.drift
    return out


def run_experiment(exp: Experiment, lab_data: Path, truth: DotTruth) -> tuple[RunResult, str]:
    lab_sessions = sessions_root(lab_data)
    out = results_dir(lab_data, exp.name)
    if out.exists():
        shutil.rmtree(out)
    work = out / "data"
    work.mkdir(parents=True)
    scores: list[Score] = []
    failures: list[Failure] = []
    for study in exp.studies:
        refs = list_sessions(lab_sessions, study)
        if not refs:
            raise FileNotFoundError(f"study {study} is not in the lab (run `lab import-study`)")
        for ref in refs:
            prepare_session(lab_sessions, work, ref)
            done, failed = run_stages(work, ref, exp.config, exp.pipelines, exp.variant)
            failures += failed
            derived = derived_dir(work, ref.study_id, ref.session_id)
            for p in done:
                scores.append(score_session(work, derived, ref, p, truth, exp.config))
    result = RunResult(scores, failures)
    overrides = {**exp.overrides, **variant_summary(exp.variant)}
    header = _header(exp.name, exp.description, overrides, exp.config,
                     {"tune_studies": list(exp.tune_studies),
                      "holdout_studies": list(exp.holdout_studies)})  # fmt: skip
    return result, write_results(out, header, result, truth)


def score_reference(
    lab_data: Path, studies: tuple[int, ...], truth: DotTruth
) -> tuple[RunResult, str]:
    """Score the product pipeline's own gaze tables (copied at import) like an experiment, as the
    pseudo-experiment `reference`: what the sessions in `data/` say today."""
    lab_sessions = sessions_root(lab_data)
    out = results_dir(lab_data, "reference")
    out.mkdir(parents=True, exist_ok=True)
    config = PipelineConfig()
    scores: list[Score] = []
    failures: list[Failure] = []
    for study in studies:
        for ref in list_sessions(lab_sessions, study):
            ref_dir = reference_dir(lab_sessions, ref.study_id, ref.session_id)
            for p, spec in SPECS.items():
                if (ref_dir / spec.gaze_file).exists():
                    scores.append(score_session(lab_sessions, ref_dir, ref, p, truth, config))
    result = RunResult(scores, failures)
    header = _header("reference", "the product pipeline's outputs as they are in data/", {},
                     config, {"studies": list(studies)})  # fmt: skip
    return result, write_results(out, header, result, truth)


def load_scores(lab_data: Path, experiment: str) -> tuple[dict[str, Any], list[Score]]:
    path = results_dir(lab_data, experiment) / SCORES_FILE
    if not path.is_file():
        raise FileNotFoundError(f"no results for '{experiment}' (run it first): {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    return data, [Score.from_dict(s) for s in data["scores"]]
