"""Other calibration models (T13 items 2, 3, 6, 7) and richer training data (item 5), in the lab.

Every model is fitted on the same training set and the same inliers as the product: the chosen
calibration attempt's dots (plus validation dots by `fit_validation`, plus video 1's rests or
pursuit by `extra_training`), the product's training windows (`training_samples`), and the
product's outlier rule applied with a first product ridge fit. The fitted model predicts every
usable frame; `run_gaze(..., prediction=...)` (DECISIONS 2026-11-01) does the rest unchanged.

Models (`model` in the experiment file):
- `ridge`: the product's own procedure (`select_model`, fit, `inlier_mask`, again); reproduces the
  product, so it is the reference for the others;
- `ridge_xy`: one product ridge for x on `x_features` and one for y on `y_features`;
- `dotmean`: ridge on each dot's median features and target (one row per dot): removes most of
  the frame noise from the inputs, which otherwise flattens the slope (errors in variables);
- `tps`: the product ridge, then a thin-plate-spline correction from the ridge's estimate to the
  target, fitted on each dot's median estimate (smoothing by leave-one-dot-out; "no correction"
  wins when it is better);
- `krr`: kernel ridge regression (RBF) on the standardised features, alpha and gamma by
  leave-one-dot-out;
- `gp`: a Gaussian process (constant x RBF with one length scale per feature, plus white noise),
  fitted by its marginal likelihood.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq
from numpy.typing import NDArray

from app.events import parse_events, parse_records
from app.schemas import EventRecord, StimulusFrameRecord
from app.storage import derived_dir, events_path, stimulus_frames_path
from lab.dots import DotVideo, truth
from lab.layout import SessionRef
from worker.pipeline.calibration import (
    calibration_attempts,
    choose_attempt,
    inlier_mask,
    select_model,
    training_samples,
)
from worker.pipeline.config import PipelineConfig
from worker.pipeline.predict import SessionPrediction, gaze_from_xy, viewport_timeline
from worker.pipeline.ridge import fit_ridge, predict
from worker.pipeline.stimulus_map import StimulusMap, VideoRectTimeline
from worker.pipeline.sync import SYNC_FILE, read_t_of_pts
from worker.pipeline.validation import validation_blocks

Floats = NDArray[np.float64]
Ints = NDArray[np.int64]
Bools = NDArray[np.bool_]

MODELS = ("product", "ridge", "ridge_xy", "dotmean", "tps", "krr", "gp")
EXTRA_TRAINING = ("none", "l1_rests", "l1_pursuit", "l1_both", "l1_pursuit_only")
EXTRA_SETTLE_S = 0.5  # frames of a rest or a pursuit segment count from this long after its start
PURSUIT_CHUNK_S = 1.0  # a pursuit segment is cut into "dots" of this length for leave-one-out
TPS_SMOOTHING = (0.0, 1e-3, 1e-2, 1e-1, 1.0, 10.0)
KRR_ALPHAS = (0.01, 0.1, 1.0, 10.0)
KRR_GAMMAS = (0.01, 0.03, 0.1, 0.3)  # times 1 / number of features


@dataclass(frozen=True)
class SessionMatrix:
    """A session's frames: client time, all feature columns, the blink flag."""

    index: Ints
    pts: Floats
    t_ms: Floats
    face: Bools
    blink: Bools
    table_columns: dict[str, Floats]

    def vectors(self, columns: Sequence[str]) -> Floats:
        return np.stack([self.table_columns[c] for c in columns], axis=1)

    def usable(self, columns: Sequence[str]) -> Bools:
        return np.isfinite(self.vectors(columns)).all(axis=1) & ~self.blink


def session_matrix(root: Path, ref: SessionRef, features_file: str) -> SessionMatrix:
    derived = derived_dir(root, ref.study_id, ref.session_id)
    table = pq.read_table(derived / features_file)
    a_ms, b = read_t_of_pts(derived / SYNC_FILE)
    pts = table["pts"].to_numpy().astype(np.float64)
    skip = {"index", "pts", "face", "blink"}
    columns = {
        c: np.array([np.nan if v is None else v for v in table[c].to_pylist()], dtype=np.float64)
        for c in table.column_names
        if c not in skip
    }
    return SessionMatrix(
        index=table["index"].to_numpy(), pts=pts, t_ms=a_ms + 1000.0 * b * pts,
        face=np.asarray(table["face"].to_pylist(), dtype=np.bool_),
        blink=np.asarray(table["blink"].to_pylist(), dtype=np.bool_), table_columns=columns,
    )  # fmt: skip


# --- The training set -------------------------------------------------------------------------


@dataclass(frozen=True)
class Training:
    rows: Ints  # frame rows of the session
    targets: Floats  # (rows, 2) viewport px
    group: Ints  # the "dot" of each row, for leave-one-dot-out


def _dot_rows(
    m: SessionMatrix,
    events: Sequence[EventRecord],
    usable: Bools,
    config: PipelineConfig,
    validation_kinds: tuple[str, ...],
) -> Training | None:
    chosen = choose_attempt(calibration_attempts(events))
    if chosen is None:
        return None
    attempt, _ = chosen
    extra = [
        d for blk in validation_blocks(events, attempt.start_t) if blk.kind in validation_kinds
        for d in blk.dots
    ]  # fmt: skip
    dots = replace(attempt, dots=[*attempt.dots, *extra]).dots
    # training_samples returns features, not rows: give it the row number as the only feature
    rows_as_feature = np.arange(len(m.t_ms), dtype=np.float64)[:, None]
    ts = training_samples(dots, m.t_ms, rows_as_feature, usable, config)
    return Training(ts.features[:, 0].astype(np.int64), ts.targets, ts.dot_index)


def _video_rows(
    root: Path,
    ref: SessionRef,
    m: SessionMatrix,
    events: Sequence[EventRecord],
    usable: Bools,
    video: DotVideo,
    kinds: tuple[str, ...],
    config: PipelineConfig,
    first_group: int,
) -> Training:
    """Frames of a dot video's rests and/or pursuit segments, with the dot as target."""
    frames_file = stimulus_frames_path(root, ref.study_id, ref.session_id)
    frames = parse_records(frames_file.read_bytes(), StimulusFrameRecord, "stimulus_frames")
    lookup = StimulusMap(frames, events, config).lookup(m.t_ms)
    vids = np.array(lookup.video_id, dtype=object)
    stim = lookup.stim_time
    t = truth(video.name)
    spans: list[tuple[float, float]] = []
    if "rests" in kinds:
        spans += [(r.start_s + EXTRA_SETTLE_S, r.end_s) for r in t.rests]
    if "pursuit" in kinds:
        for a, b in t.pursuits:
            start = a + EXTRA_SETTLE_S
            while start < b:
                spans.append((start, min(start + PURSUIT_CHUNK_S, b)))
                start += PURSUIT_CHUNK_S
    rects = VideoRectTimeline(events)
    width, height = rects.rect_at(m.t_ms)
    origin_x, origin_y = rects.normalise(m.t_ms, np.zeros(len(m.t_ms)), np.zeros(len(m.t_ms)))
    rows, targets, groups = [], [], []
    mine = usable & (vids == video.video_id) & np.isfinite(stim)
    for k, (a, b) in enumerate(spans):
        sel = np.flatnonzero(mine & (stim >= a) & (stim <= b))
        if len(sel) == 0:
            continue
        fx, fy = t.path.at(stim[sel])
        x = (fx - origin_x[sel]) * width[sel]  # video fraction to viewport px
        y = (fy - origin_y[sel]) * height[sel]
        rows.append(sel)
        targets.append(np.stack([x, y], axis=1))
        groups.append(np.full(len(sel), first_group + k, dtype=np.int64))
    if not rows:
        return Training(np.empty(0, np.int64), np.empty((0, 2)), np.empty(0, np.int64))
    return Training(np.concatenate(rows), np.concatenate(targets), np.concatenate(groups))


def build_training(
    root: Path,
    ref: SessionRef,
    m: SessionMatrix,
    usable: Bools,
    config: PipelineConfig,
    validation_kinds: tuple[str, ...],
    extra: str,
    dots: Sequence[DotVideo],
) -> Training | None:
    events = parse_events(events_path(root, ref.study_id, ref.session_id).read_bytes())
    base = None if extra == "l1_pursuit_only" else _dot_rows(m, events, usable, config,
                                                             validation_kinds)  # fmt: skip
    if extra == "none":
        return base
    if len(dots) < 2:
        raise ValueError(f"{ref.code}: extra_training needs two dot videos (video 2 is the test)")
    kinds = {"l1_rests": ("rests",), "l1_pursuit": ("pursuit",), "l1_both": ("rests", "pursuit"),
             "l1_pursuit_only": ("pursuit",)}[extra]  # fmt: skip
    first = 0 if base is None else int(base.group.max()) + 1
    more = _video_rows(root, ref, m, events, usable, dots[0], kinds, config, first)
    if base is None:
        return more
    return Training(
        np.concatenate([base.rows, more.rows]), np.concatenate([base.targets, more.targets]),
        np.concatenate([base.group, more.group]),
    )  # fmt: skip


# --- Models: fit on (features, targets, groups), return a predictor ---------------------------

Predictor = Callable[[Floats], Floats]


def fit_product_ridge(x: Floats, y: Floats, g: Ints, config: PipelineConfig) -> Predictor:
    """The product's procedure: choose, fit, drop outliers, choose and fit again."""
    first = select_model(x, y, g, config)
    model = fit_ridge(x, y, first.chosen.alpha, first.chosen.degree)
    keep = inlier_mask(model, x, g, config)
    chosen = select_model(x[keep], y[keep], g[keep], config).chosen
    final = fit_ridge(x[keep], y[keep], chosen.alpha, chosen.degree)
    return lambda rows: predict(final, rows)


def _inliers(x: Floats, y: Floats, g: Ints, config: PipelineConfig) -> Bools:
    first = select_model(x, y, g, config)
    return inlier_mask(fit_ridge(x, y, first.chosen.alpha, first.chosen.degree), x, g, config)


def fit_ridge_xy(
    xx: Floats, xy: Floats, y: Floats, g: Ints, config: PipelineConfig
) -> Callable[[Floats, Floats], Floats]:
    """x from the columns `xx`, y from `xy`: two product ridges, each scored on its own axis (the
    other target column is held at 0, so the 2D CV error is the axis error)."""
    zeros = np.zeros(len(y))
    px = fit_product_ridge(xx, np.stack([y[:, 0], zeros], axis=1), g, config)
    py = fit_product_ridge(xy, np.stack([y[:, 1], zeros], axis=1), g, config)
    return lambda rx, ry: np.stack([px(rx)[:, 0], py(ry)[:, 0]], axis=1)


def _group_medians(x: Floats, y: Floats, g: Ints) -> tuple[Floats, Floats, Ints]:
    ids = np.unique(g)
    mx = np.stack([np.median(x[g == k], axis=0) for k in ids])
    my = np.stack([np.median(y[g == k], axis=0) for k in ids])
    return mx, my, ids


def fit_dotmean(x: Floats, y: Floats, g: Ints, config: PipelineConfig) -> Predictor:
    keep = _inliers(x, y, g, config)
    mx, my, _ = _group_medians(x[keep], y[keep], g[keep])
    chosen = select_model(mx, my, np.arange(len(mx)), config).chosen
    model = fit_ridge(mx, my, chosen.alpha, chosen.degree)
    return lambda rows: predict(model, rows)


def _tps(points: Floats, values: Floats, smoothing: float) -> Callable[[Floats], Floats]:
    from scipy.interpolate import RBFInterpolator

    rbf = RBFInterpolator(points, values, kernel="thin_plate_spline", smoothing=smoothing)
    return lambda p: np.asarray(rbf(p), dtype=np.float64)


def fit_tps(x: Floats, y: Floats, g: Ints, config: PipelineConfig) -> Predictor:
    """Ridge, then a thin-plate-spline correction of its estimate (screen units of 1000 px)."""
    base = fit_product_ridge(x, y, g, config)
    keep = _inliers(x, y, g, config)
    est, target, _ = _group_medians(base(x[keep]), y[keep], g[keep])
    scale = 1000.0
    residual = target - est
    best: tuple[float, float | None] = (float(np.linalg.norm(residual, axis=1).mean()), None)
    if len(est) >= 6:
        for s in TPS_SMOOTHING:
            errors = []
            for k in range(len(est)):
                train = np.arange(len(est)) != k
                corr = _tps(est[train] / scale, residual[train], s)(est[[k]] / scale)
                errors.append(float(np.linalg.norm(residual[k] - corr[0])))
            if (e := float(np.mean(errors))) < best[0]:
                best = (e, s)
    if best[1] is None:
        return base
    correction = _tps(est / scale, residual, best[1])
    return lambda rows: (lambda p: p + correction(p / scale))(base(rows))


def _standardise(x: Floats) -> tuple[Floats, Floats]:
    mean, std = x.mean(axis=0), x.std(axis=0)
    std[std == 0] = 1.0
    return mean, std


def fit_krr(x: Floats, y: Floats, g: Ints, config: PipelineConfig) -> Predictor:
    from sklearn.kernel_ridge import KernelRidge

    keep = _inliers(x, y, g, config)
    x, y, g = x[keep], y[keep], g[keep]
    mean, std = _standardise(x)
    z = (x - mean) / std
    ym = y.mean(axis=0)
    groups = np.unique(g)
    best: tuple[float, float, float] = (float("inf"), 1.0, 0.1)
    for alpha in KRR_ALPHAS:
        for gamma in KRR_GAMMAS:
            gm = gamma / x.shape[1]
            err = 0.0
            for k in groups:
                train = g != k
                m = KernelRidge(alpha=alpha, kernel="rbf", gamma=gm).fit(z[train], y[train] - ym)
                err += float(np.linalg.norm(m.predict(z[~train]) + ym - y[~train], axis=1).mean())
            if err / len(groups) < best[0]:
                best = (err / len(groups), alpha, gm)
    model = KernelRidge(alpha=best[1], kernel="rbf", gamma=best[2]).fit(z, y - ym)
    return lambda rows: np.asarray(model.predict((rows - mean) / std), dtype=np.float64) + ym


def fit_gp(x: Floats, y: Floats, g: Ints, config: PipelineConfig) -> Predictor:
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import RBF, ConstantKernel, WhiteKernel

    keep = _inliers(x, y, g, config)
    x, y = x[keep], y[keep]
    mean, std = _standardise(x)
    rbf = RBF(length_scale=np.ones(x.shape[1]) * 3.0, length_scale_bounds=(0.1, 100.0))
    kernel = ConstantKernel(1.0) * rbf + WhiteKernel(0.1)
    model = GaussianProcessRegressor(kernel=kernel, normalize_y=True, random_state=0)
    model.fit((x - mean) / std, y)
    return lambda rows: np.asarray(model.predict((rows - mean) / std), dtype=np.float64)


# --- The session's prediction -------------------------------------------------------------------


@dataclass(frozen=True)
class ModelSpec:
    model: str
    columns: tuple[str, ...]
    x_columns: tuple[str, ...] = ()
    y_columns: tuple[str, ...] = ()
    validation_kinds: tuple[str, ...] = ()
    extra: str = "none"


def predict_with_model(
    root: Path,
    ref: SessionRef,
    features_file: str,
    spec: ModelSpec,
    config: PipelineConfig,
    dots: Sequence[DotVideo],
) -> SessionPrediction:
    """Fit `spec.model` on the session's training set and predict every usable frame."""
    m = session_matrix(root, ref, features_file)
    needed = spec.columns if spec.model != "ridge_xy" else (*spec.x_columns, *spec.y_columns)
    usable = m.usable(needed)
    training = build_training(root, ref, m, usable, config, spec.validation_kinds, spec.extra, dots)
    if training is None or len(training.rows) == 0:
        raise ValueError(f"{ref.code}: no training samples")
    y, g = training.targets, training.group
    x_all, y_all = np.full(len(m.t_ms), np.nan), np.full(len(m.t_ms), np.nan)
    if spec.model == "ridge_xy":
        xx, xy = m.vectors(spec.x_columns), m.vectors(spec.y_columns)
        fitted = fit_ridge_xy(xx[training.rows], xy[training.rows], y, g, config)
        out = fitted(xx[usable], xy[usable])
    else:
        x = m.vectors(spec.columns)
        fit = {"ridge": fit_product_ridge, "dotmean": fit_dotmean, "tps": fit_tps,
               "krr": fit_krr, "gp": fit_gp}[spec.model]  # fmt: skip
        out = fit(x[training.rows], y, g, config)(x[usable])
    x_all[usable], y_all[usable] = out[:, 0], out[:, 1]
    events = parse_events(events_path(root, ref.study_id, ref.session_id).read_bytes())
    gaze = gaze_from_xy(x_all, y_all, usable, m.t_ms, viewport_timeline(events), config)
    return SessionPrediction(
        index=m.index, pts=m.pts, t_ms=m.t_ms, face=m.face, blink=m.blink, usable=usable, gaze=gaze
    )
