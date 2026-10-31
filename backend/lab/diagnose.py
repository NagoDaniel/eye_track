"""E1 diagnostics (T13): measure where the error comes from, change nothing.

Reads an experiment's outputs (normally `baseline`) and writes `results/diagnose/report.txt`:

1. **Head pose and distance, calibration against playback.** The regression standardises each
   feature with the mean and spread of the calibration samples (`RidgeModel.means`, `.stds`). If
   the head hardly moved during calibration, a small movement later is many "calibration spreads"
   away, and the fitted weight multiplies it. Per feature: the calibration spread, and the
   playback frames in units of that spread (median and 95th percentile of |z|, the shift of the
   median). `tx, ty, tz` are not regressors; their calibration spread is taken from the
   calibration-phase frames.
2. **What moves the gaze.** The model is linear (every session so far), so a prediction is exactly
   intercept + sum of weight * z over the features. Per feature group (iris, lids, head angles, face
   size): its share in the change of the prediction between the first and the last rest of the dot
   (both at the centre, so the true gaze did not move), and how much it moves the gaze during
   playback (spread of its contribution).
3. **Timing.** `t - capture_time` of the browser feature records (how late a webcam frame is
   stamped) against the best gaze lag of the scores.
4. **Regularisation.** The pipeline re-run with each alpha alone (`calib_alphas = [alpha]`): median
   pause error, slopes of gaze on dot (centre pull) and the leave-one-dot-out error.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
from numpy.typing import NDArray

from app.storage import browser_features_path, derived_dir
from lab.experiment import parse_experiment
from lab.layout import SessionRef, list_sessions, results_dir
from lab.run import load_scores, run_experiment
from lab.score import REST_SETTLE_S, DotTruth, Score
from lab.variants import HP_SUFFIX
from worker.pipeline.features_a import FEATURES_FILE
from worker.pipeline.ridge import RidgeModel
from worker.pipeline.spec import SPEC_A

Floats = NDArray[np.float64]

GROUPS: dict[str, tuple[str, ...]] = {
    "iris": ("iris_l_x", "iris_l_y", "iris_r_x", "iris_r_y"),
    "lids": ("open_l", "open_r"),
    "head angles": ("yaw", "pitch", "roll"),
    "face size": ("face_ratio",),
    "head position": ("tx", "ty", "tz"),
}
POSE_COLUMNS = ("yaw", "pitch", "roll", "face_ratio", "tx", "ty", "tz")
ALPHAS = (0.01, 0.1, 0.3, 1.0, 3.0, 10.0, 30.0, 100.0)
FIRST_REST, LAST_REST = 0, -1  # both at the centre of the moving-dot video


def _floats(table: pa.Table, name: str) -> Floats:
    return np.array([np.nan if v is None else v for v in table[name].to_pylist()], dtype=np.float64)


# --- 2. contributions ---


def contributions(model: RidgeModel, vectors: Floats) -> Floats:
    """(frames, features, 2): weight * z of each feature for x and y. Their sum over the features
    plus the intercepts is the prediction (linear model only)."""
    if model.degree != 1:
        raise ValueError("the split by feature is exact only for the linear model")
    z = (vectors - model.means) / model.stds
    out: Floats = z[:, :, None] * model.weights[None, :, :]
    return out


def group_sum(per_feature: Floats, columns: Sequence[str]) -> dict[str, Floats]:
    """Contributions summed over each group: name -> (frames, 2). A high-passed column
    (`open_l_hp`) counts in the group of its base column; a group without columns sums to 0."""
    out = {}
    for g, cols in GROUPS.items():
        mine = [i for i, c in enumerate(columns) if c.removesuffix(HP_SUFFIX) in cols]
        out[g] = per_feature[:, mine, :].sum(axis=1)
    return out


@dataclass(frozen=True)
class SessionDiagnosis:
    study_id: int
    code: str
    # 1. per pose column: calibration spread, |z| median and p95 in playback, median z shift
    pose: dict[str, tuple[float, float, float, float]]
    # 2. per group: change of its contribution (x, y) from the first to the last centre rest, and
    # its spread (x, y) during playback; viewport px
    rest_change: dict[str, tuple[float, float]]
    total_change: tuple[float, float]  # change of the prediction (= of the error) between them
    playback_spread: dict[str, tuple[float, float]]
    # 3. t - capture_time, ms: median, p5, p95; None when the browser gave no capture time
    stamp_delay_ms: tuple[float, float, float] | None


def _rest_rows(stim: Floats, ok: NDArray[np.bool_], truth: DotTruth, k: int) -> NDArray[np.bool_]:
    r = truth.rests[k]
    with np.errstate(invalid="ignore"):
        return ok & (stim >= r.start_s + REST_SETTLE_S) & (stim <= r.end_s)


def diagnose_session(root: Path, ref: SessionRef, truth: DotTruth) -> SessionDiagnosis:
    derived = derived_dir(root, ref.study_id, ref.session_id)
    features = pq.read_table(derived / FEATURES_FILE)
    gaze = pq.read_table(derived / SPEC_A.gaze_file)
    if features["pts"].to_pylist() != gaze["webcam_pts"].to_pylist():
        raise ValueError(f"{ref.code}: features and gaze rows do not line up")
    calibration = json.loads((derived / SPEC_A.calibration_file).read_text(encoding="utf-8"))
    model = RidgeModel.from_dict(calibration["model"])
    columns = list(calibration["feature_columns"])

    phase = np.asarray(gaze["phase"].to_pylist(), dtype=object)
    valid = np.asarray(gaze["valid"].to_pylist(), dtype=bool)
    stim = _floats(gaze, "stim_time")
    playback = (phase == "playback") & valid
    calib = (phase == "calibration") & valid

    pose = {}
    for c in POSE_COLUMNS:
        x = _floats(features, c)
        if c in columns:
            mean, std = model.means[columns.index(c)], model.stds[columns.index(c)]
        else:
            mean, std = float(np.nanmean(x[calib])), float(np.nanstd(x[calib]))
        z = (x[playback] - mean) / std
        z = z[np.isfinite(z)]
        pose[c] = (float(std), float(np.median(np.abs(z))), float(np.percentile(np.abs(z), 95)),
                   float(np.median(z)))  # fmt: skip

    vectors = np.stack([_floats(features, c) for c in columns], axis=1)
    groups = group_sum(contributions(model, vectors), columns)
    first = _rest_rows(stim, playback, truth, FIRST_REST)
    last = _rest_rows(stim, playback, truth, LAST_REST % len(truth.rests))
    rest_change, spread = {}, {}
    for g, values in groups.items():
        d = np.median(values[last], axis=0) - np.median(values[first], axis=0)
        rest_change[g] = (float(d[0]), float(d[1]))
        s = np.std(values[playback], axis=0)
        spread[g] = (float(s[0]), float(s[1]))
    total = np.zeros(2)
    for v in groups.values():
        total += np.median(v[last], axis=0) - np.median(v[first], axis=0)
    return SessionDiagnosis(
        ref.study_id, ref.code, pose, rest_change, (float(total[0]), float(total[1])), spread,
        stamp_delay(browser_features_path(root, ref.study_id, ref.session_id)),
    )  # fmt: skip


# --- 3. timing ---


def stamp_delay(path: Path) -> tuple[float, float, float] | None:
    """`t - capture_time` over the browser's feature records (ms)."""
    delays = []
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("capture_time") is not None:
            delays.append(float(r["t"]) - float(r["capture_time"]))
    if not delays:
        return None
    d = np.array(delays)
    return float(np.median(d)), float(np.percentile(d, 5)), float(np.percentile(d, 95))


# --- 4. alpha sweep ---


@dataclass(frozen=True)
class AlphaRow:
    alpha: float
    study_id: int
    pause: float  # median over the sessions, viewport px
    dc_pause: float | None
    slope_x: float | None  # median over the sessions
    slope_y: float | None
    cv_error: float  # median leave-one-dot-out error of the chosen model, viewport px


def _median(values: Sequence[float | None]) -> float | None:
    known = [v for v in values if v is not None]
    return float(np.median(known)) if known else None


def alpha_sweep(
    lab_data: Path, studies: tuple[int, ...], truth: DotTruth, alphas: Sequence[float] = ALPHAS
) -> list[AlphaRow]:
    rows = []
    for alpha in alphas:
        name = "diag-alpha-" + f"{alpha:g}".replace(".", "p")
        exp = parse_experiment({
            "name": name, "description": f"diagnose: ridge alpha fixed at {alpha:g}",
            "pipelines": ["a"], "tune_studies": list(studies), "config": {"calib_alphas": [alpha]},
        })  # fmt: skip
        result, _ = run_experiment(exp, lab_data, truth)
        work = results_dir(lab_data, name) / "data"
        for study in studies:
            mine = [s for s in result.scores if s.study_id == study]
            cv = []
            for ref in list_sessions(work, study):
                path = derived_dir(work, study, ref.session_id) / SPEC_A.calibration_file
                cv.append(float(json.loads(path.read_text(encoding="utf-8"))["cv"]["error_px"]))
            rows.append(AlphaRow(
                alpha, study, float(np.median([s.pause for s in mine])),
                _median([s.dc_pause for s in mine]),
                _median([None if s.x is None else s.x.slope for s in mine]),
                _median([None if s.y is None else s.y.slope for s in mine]),
                float(np.median(cv)),
            ))  # fmt: skip
    return rows


# --- The report ---


def format_diagnosis(
    sessions: Sequence[SessionDiagnosis], scores: Sequence[Score], alphas: Sequence[AlphaRow]
) -> str:
    lag = {(s.study_id, s.code): s.lag_ms for s in scores if s.pipeline == "a"}
    lines = ["1. head pose and distance: calibration spread (sd), playback in units of it (z)",
             "   |z| med / p95 = how far playback frames are from the calibration mean; "
             "shift = median z", ""]  # fmt: skip
    head = f"{'study':>5} {'code':8}" + "".join(f"| {c:^24}" for c in POSE_COLUMNS)
    lines += [head, f"{'':14}" + "| {:>6} {:>5} {:>5} {:>5} ".format("sd", "|z|", "p95", "shift")
              * len(POSE_COLUMNS)]  # fmt: skip
    for d in sessions:
        line = f"{d.study_id:5} {d.code:8}"
        for c in POSE_COLUMNS:
            sd, med, p95, shift = d.pose[c]
            line += f"| {sd:6.3g} {med:5.1f} {p95:5.1f} {shift:+5.1f} "
        lines.append(line)
    lines += [
        "",
        "2. what moves the gaze (linear model: prediction = sum of weight * z), viewport px",
        "   change from the first to the last centre rest (true gaze unchanged, so this is the",
        "   drift), by feature group, x / y; then each group's spread during playback, x / y",
        "",
    ]
    groups = list(GROUPS)
    lines.append(f"{'study':>5} {'code':8}| {'total':^13}" + "".join(f"| {g:^13}" for g in groups)
                 + " || spread: " + " ".join(f"{g:>13}" for g in groups))  # fmt: skip
    for d in sessions:
        line = f"{d.study_id:5} {d.code:8}| {d.total_change[0]:+5.0f} {d.total_change[1]:+5.0f}  "
        for g in groups:
            line += f"| {d.rest_change[g][0]:+5.0f} {d.rest_change[g][1]:+5.0f}  "
        line += " ||         " + " ".join(
            f"{d.playback_spread[g][0]:6.0f}{d.playback_spread[g][1]:6.0f} " for g in groups
        )
        lines.append(line)
    lines += ["", "3. timing: t - capture_time of the browser feature records (ms) against the "
              "best gaze lag (pipeline A)", ""]  # fmt: skip
    lines.append(f"{'study':>5} {'code':8}{'median':>8}{'p5':>7}{'p95':>7}{'gaze lag':>10}")
    for d in sessions:
        stamp = d.stamp_delay_ms
        delay = (
            "   (no capture_time)"
            if stamp is None
            else f"{stamp[0]:8.1f}{stamp[1]:7.1f}{stamp[2]:7.1f}"
        )
        gl = lag.get((d.study_id, d.code))
        lines.append(f"{d.study_id:5} {d.code:8}{delay}{'-' if gl is None else f'{gl:+.0f}':>10}")
    lines += ["", "4. ridge alpha fixed (degree still chosen by CV), medians over the sessions",
              "   pause / dc pause: viewport px; slope: gaze on dot (1 = no centre pull); "
              "cv: leave-one-dot-out error, viewport px", ""]  # fmt: skip
    lines.append(f"{'alpha':>7}{'study':>6}{'pause':>7}{'dc pause':>9}{'slope x':>8}{'slope y':>8}"
                 f"{'cv':>6}")  # fmt: skip
    for r in alphas:
        lines.append(
            f"{r.alpha:7g}{r.study_id:6}{r.pause:7.0f}"
            f"{'-' if r.dc_pause is None else f'{r.dc_pause:.0f}':>9}"
            f"{'-' if r.slope_x is None else f'{r.slope_x:.2f}':>8}"
            f"{'-' if r.slope_y is None else f'{r.slope_y:.2f}':>8}{r.cv_error:6.0f}"
        )
    return "\n".join(lines) + "\n"


def run_diagnosis(
    lab_data: Path, experiment: str, truth: DotTruth, sweep: bool = True
) -> tuple[list[SessionDiagnosis], str]:
    header, scores = load_scores(lab_data, experiment)
    studies = tuple(header.get("tune_studies", [])) + tuple(header.get("holdout_studies", []))
    work = results_dir(lab_data, experiment) / "data"
    sessions = [
        diagnose_session(work, ref, truth) for st in studies for ref in list_sessions(work, st)
    ]
    alphas = alpha_sweep(lab_data, studies, truth) if sweep else []
    report = f"diagnosis of experiment '{experiment}'\n\n" + format_diagnosis(
        sessions, scores, alphas
    )
    out = results_dir(lab_data, "diagnose")
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.txt").write_text(report, encoding="utf-8")
    return sessions, report
