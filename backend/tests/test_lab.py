"""The T13 experiment lab (`backend/lab`, DECISIONS 2026-10-30).

Synthetic data with known answers for the scoring, the comparison rule and the experiment files;
the real recorded fixture session (real MediaPipe, real ffmpeg) for the import and the baseline run,
which must reproduce the product pipeline's outputs exactly.
"""

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.schemas import EventRecord
from app.storage import derived_dir, raw_dir
from lab.compare import MAX_WORSE_FRACTION, format_comparison, verdict
from lab.experiment import ExperimentError, load_experiment, parse_experiment
from lab.importer import import_study
from lab.layout import DOT_PATH_FILE, EXPERIMENTS_DIR, reference_dir, results_dir, sessions_root
from lab.run import SCORES_FILE, load_scores, run_experiment
from lab.score import AxisFit, DotTruth, Score, score_gaze
from tests.session_fixture import FixtureSession
from worker.pipeline.config import PipelineConfig
from worker.pipeline.features_a import FEATURE_VECTOR_COLUMNS
from worker.pipeline.spec import SPEC_A

TRUTH = DotTruth.load(DOT_PATH_FILE)
FPS = 30
SCALE = 2.0  # the video's rectangle on screen is twice the video's own size


def _events() -> list[EventRecord]:
    w, h = TRUTH.width * SCALE, TRUTH.height * SCALE
    size = {"width": w, "height": h}
    data = {"viewport": size, "screen": size, "device_pixel_ratio": 1,
            "video_rect": {"x": 0.0, "y": 0.0, **size}}  # fmt: skip
    return [EventRecord.model_validate({"t": 0, "type": "viewport_change", "data": data})]


def _gaze(gain: float = 1.0, offset_px: tuple[float, float] = (0.0, 0.0)) -> pa.Table:
    """The participant's gaze = gain * (dot - centre) + centre + offset, in video px, every frame
    valid, as video-normalised coordinates."""
    stim = np.arange(0.0, TRUTH.path.duration_s - 0.05, 1 / FPS)
    tx, ty = TRUTH.path.at(stim)
    x = (gain * (tx - 0.5) + 0.5) + offset_px[0] / TRUTH.width
    y = (gain * (ty - 0.5) + 0.5) + offset_px[1] / TRUTH.height
    n = len(stim)
    return pa.table({
        "t": pa.array(1000 + stim * 1000), "stim_time": pa.array(stim),
        "x_vid": pa.array(x), "y_vid": pa.array(y),
        "x_vid_dc": pa.array(x), "y_vid_dc": pa.array(y),
        "valid": pa.array([True] * n), "phase": pa.array(["playback"] * n),
        "video_id": pa.array(["1"] * n),
    })  # fmt: skip


def _score(table: pa.Table) -> Score:
    return score_gaze(table, _events(), TRUTH, PipelineConfig(), 7, "XX0101", "a")


# --- Scoring against the dot ---


def test_gaze_on_the_dot_scores_zero_error_and_unit_slope() -> None:
    s = _score(_gaze())
    assert s.median == pytest.approx(0, abs=1e-6)
    assert s.pause == pytest.approx(0, abs=1e-6) and s.dc_pause == pytest.approx(0, abs=1e-6)
    assert s.lag_ms == 0
    assert s.x is not None and s.y is not None
    assert s.x.slope == pytest.approx(1) and s.y.slope == pytest.approx(1)
    assert s.x.r == pytest.approx(1) and s.y.r == pytest.approx(1)
    assert all(e is not None and np.hypot(*e) < 1e-6 for e in s.rest_error)
    assert s.viewport_per_video_px == pytest.approx(SCALE)


def test_a_constant_offset_is_reported_in_viewport_px_in_table_1_and_video_px_elsewhere() -> None:
    s = _score(_gaze(offset_px=(40.0, -20.0)))
    assert s.median == pytest.approx(SCALE * np.hypot(40, 20), rel=1e-6)  # viewport px
    for e in s.rest_error:  # video px
        assert e is not None
        assert e[0] == pytest.approx(40, abs=1e-6) and e[1] == pytest.approx(-20, abs=1e-6)
    assert s.x is not None
    assert s.x.intercept == pytest.approx(40, abs=1e-6) and s.x.slope == pytest.approx(1)


def test_a_pull_to_the_centre_shows_as_a_slope_below_one() -> None:
    s = _score(_gaze(gain=0.5))
    assert s.x is not None and s.y is not None
    assert s.x.slope == pytest.approx(0.5) and s.y.slope == pytest.approx(0.5)
    assert s.x.intercept == pytest.approx(TRUTH.width / 4, abs=1e-6)  # 0.5 * (x - w/2) + w/2
    corner = s.rest_error[8]  # (1152, 648): gaze falls short towards the centre
    assert corner is not None and corner[0] < 0 and corner[1] < 0


def test_the_affine_check_removes_a_constant_gain_and_offset() -> None:
    s = _score(_gaze(gain=0.8, offset_px=(30.0, 10.0)))
    assert s.affine_before is not None and s.affine_before > 20
    assert s.affine_after is not None and s.affine_after == pytest.approx(0, abs=1e-6)


def test_scores_survive_a_round_trip_through_json() -> None:
    s = _score(_gaze(offset_px=(5.0, 5.0)))
    assert Score.from_dict(json.loads(json.dumps(s.as_dict()))) == s


# --- The comparison rule ---


def _fake(study: int, code: str, pause: float, dc_pause: float | None = None) -> Score:
    fit = AxisFit(1.0, 0.0, 1.0)
    return Score(
        study_id=study, code=code, pipeline="a", valid_fraction=1.0, median=pause, pause=pause,
        move=pause, dc_median=dc_pause, dc_pause=dc_pause, dc_move=dc_pause, dc_p90=dc_pause,
        lag_ms=0.0, within=1.0, viewport_per_video_px=1.5, x=fit, y=fit, rest_error=(),
        affine_before=None, affine_after=None,
    )  # fmt: skip


BASE = [_fake(7, "A", 200), _fake(7, "B", 100), _fake(7, "C", 300), _fake(6, "D", 150)]


def test_better_on_the_tuning_median_with_no_session_much_worse_is_better() -> None:
    new = [_fake(7, "A", 150), _fake(7, "B", 105), _fake(7, "C", 280), _fake(6, "D", 140)]
    v = verdict(BASE, new, "pause", "a", [7], [6])
    assert (v.tune_base, v.tune_new) == (200, 150)
    assert v.better and not v.worse


def test_one_session_worse_by_more_than_the_limit_is_not_better() -> None:
    worse = 100 * (1 + MAX_WORSE_FRACTION) + 1
    new = [_fake(7, "A", 150), _fake(7, "B", worse), _fake(7, "C", 280), _fake(6, "D", 140)]
    v = verdict(BASE, new, "pause", "a", [7], [6])
    assert not v.better and [r.key for r in v.worse] == [(7, "B", "a")]


def test_a_worse_held_out_median_is_not_better() -> None:
    new = [_fake(7, "A", 150), _fake(7, "B", 100), _fake(7, "C", 280), _fake(6, "D", 160)]
    assert not verdict(BASE, new, "pause", "a", [7], [6]).better


def test_a_session_without_a_score_is_not_better() -> None:
    new = [_fake(7, "A", 150), _fake(7, "B", 90), _fake(6, "D", 140)]
    v = verdict(BASE, new, "pause", "a", [7], [6])
    assert not v.better and v.missing == [(7, "C", "a")]


def test_the_comparison_prints_every_session_and_the_verdict() -> None:
    text = format_comparison("baseline", BASE, "x", BASE, [7], [6])
    assert all(f" {c} " in text for c in "ABCD")
    assert "not better" in text  # equal is not better


# --- Experiment files ---


def test_the_baseline_experiment_is_the_default_config() -> None:
    exp = load_experiment(EXPERIMENTS_DIR / "baseline.toml")
    assert exp.config == PipelineConfig() and exp.overrides == {}
    assert exp.tune_studies == (7,) and exp.holdout_studies == (6,)


def test_overrides_take_the_type_of_the_default() -> None:
    overrides = {"calib_alphas": [1, 10], "smooth_meas_std_px": 20}
    exp = parse_experiment({"name": "x", "tune_studies": [7], "config": overrides})
    assert exp.config.calib_alphas == (1.0, 10.0)
    assert exp.config == replace(PipelineConfig(), calib_alphas=(1.0, 10.0),
                                 smooth_meas_std_px=20.0)  # fmt: skip


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"name": "x", "tune_studies": [7], "confg": {}}, "unknown keys: confg"),
        ({"name": "x", "tune_studies": [7], "config": {"no_such": 1}}, "unknown PipelineConfig"),
        ({"name": "x", "tune_studies": [7], "config": {"calib_alphas": "1"}}, "calib_alphas"),
        ({"name": "x", "tune_studies": [7], "config": {"sync_rate_hz": True}}, "sync_rate_hz"),
        ({"name": "x", "tune_studies": [7], "holdout_studies": [7]}, "both"),
        ({"name": "x"}, "tune_studies"),
        ({"name": "a b", "tune_studies": [7]}, "name"),
        ({"name": "x", "tune_studies": [7], "pipelines": ["c"]}, "pipelines"),
    ],
)
def test_wrong_experiment_files_are_refused(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ExperimentError, match=message):
        parse_experiment(data)


# --- Import and the baseline run, on the real fixture session ---


def _tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def test_the_lab_baseline_reproduces_the_product_pipeline_on_the_real_fixture(
    a_session: FixtureSession, tmp_path: Path
) -> None:
    source = a_session.root
    before = _tree_hash(source)
    lab = tmp_path / "lab_data"
    imported = import_study(source, sessions_root(lab), a_session.study_id)
    assert [i.status for i in imported] == ["imported"]
    sid, study = a_session.session_id, a_session.study_id

    # the copy: raw without the upload chunks, read-only; cached and reference files present
    raw = raw_dir(sessions_root(lab), study, sid)
    assert not (raw / "chunks").exists() and (raw / "events.jsonl").is_file()
    with pytest.raises(PermissionError):
        (raw / "events.jsonl").open("ab")
    assert (derived_dir(sessions_root(lab), study, sid) / "landmarks.parquet").is_file()
    assert (reference_dir(sessions_root(lab), study, sid) / SPEC_A.gaze_file).is_file()
    assert import_study(source, sessions_root(lab), study)[0].status == "exists"

    exp = parse_experiment({"name": "baseline", "pipelines": ["a", "b"], "tune_studies": [study]})
    result, report = run_experiment(exp, lab, TRUTH)
    assert result.failures == []  # B has no features_b here: skipped, not failed
    assert [(s.code, s.pipeline) for s in result.scores] == [(imported[0].session.code, "a")]
    assert "1. distance to the dot" in report

    product = derived_dir(source, study, sid)
    mine = derived_dir(results_dir(lab, "baseline") / "data", study, sid)
    a, b = pq.read_table(mine / SPEC_A.gaze_file), pq.read_table(product / SPEC_A.gaze_file)
    assert a.column_names == b.column_names and a.num_rows == b.num_rows
    for name in a.column_names:
        x, y = a[name].to_pylist(), b[name].to_pylist()
        if a.schema.field(name).type in (pa.float32(), pa.float64()):
            xa = np.array([np.nan if v is None else v for v in x], dtype=np.float64)
            ya = np.array([np.nan if v is None else v for v in y], dtype=np.float64)
            np.testing.assert_allclose(xa, ya, rtol=0, atol=1e-6, err_msg=name)
        else:
            assert x == y, name
    model = json.loads((mine / SPEC_A.calibration_file).read_text())["model"]
    assert model == json.loads((product / SPEC_A.calibration_file).read_text())["model"]

    header, scores = load_scores(lab, "baseline")
    assert scores == result.scores and header["tune_studies"] == [study]
    assert (results_dir(lab, "baseline") / SCORES_FILE).is_file()
    assert _tree_hash(source) == before  # the source data root was only read


# --- E1 diagnostics ---


def test_contributions_add_up_to_the_linear_prediction() -> None:
    from lab.diagnose import contributions, group_sum
    from worker.pipeline.ridge import fit_ridge, predict

    rng = np.random.default_rng(1)
    columns = list(FEATURE_VECTOR_COLUMNS)
    x = rng.normal(size=(200, len(columns)))
    y = x[:, :2] @ np.array([[300.0, 0.0], [0.0, 200.0]]) + 600
    model = fit_ridge(x, y, alpha=1.0, degree=1)
    per_feature = contributions(model, x)
    np.testing.assert_allclose(per_feature.sum(axis=1) + model.intercepts, predict(model, x))
    groups = group_sum(per_feature, columns)
    np.testing.assert_allclose(sum(groups.values()), per_feature.sum(axis=1))


def test_the_split_is_refused_for_a_quadratic_model() -> None:
    from lab.diagnose import contributions
    from worker.pipeline.ridge import fit_ridge

    x = np.random.default_rng(2).normal(size=(50, 3))
    with pytest.raises(ValueError, match="linear"):
        contributions(fit_ridge(x, x[:, :2], alpha=1.0, degree=2), x)


def test_stamp_delay_is_t_minus_capture_time(tmp_path: Path) -> None:
    from lab.diagnose import stamp_delay

    rows: list[dict[str, float | None]] = [
        {"t": 100.0 + k, "capture_time": 90.0 + k} for k in range(20)
    ]
    rows.append({"t": 500.0, "capture_time": None})
    path = tmp_path / "f.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert stamp_delay(path) == (10.0, 10.0, 10.0)
    path.write_text(json.dumps({"t": 1.0, "capture_time": None}) + "\n")
    assert stamp_delay(path) is None


def test_contributions_add_up_on_real_fixture_features(a_session: FixtureSession) -> None:
    """Real MediaPipe features (near-constant columns, real scales): a linear model fitted on them
    is split exactly. The fixture's own calibration chose the quadratic model, so the linear one is
    fitted here, on the frames the pipeline predicted."""
    from lab.diagnose import contributions
    from worker.pipeline.features_a import FEATURES_FILE
    from worker.pipeline.ridge import fit_ridge, predict

    derived = derived_dir(a_session.root, a_session.study_id, a_session.session_id)
    features = pq.read_table(derived / FEATURES_FILE)
    gaze = pq.read_table(derived / SPEC_A.gaze_file)
    vectors = np.stack(
        [np.array(features[c].to_pylist(), dtype=np.float64) for c in FEATURE_VECTOR_COLUMNS],
        axis=1,
    )
    xy = np.stack(
        [
            np.array([np.nan if v is None else v for v in gaze[c].to_pylist()])
            for c in ("x_px", "y_px")
        ],
        axis=1,
    )
    ok = np.isfinite(xy).all(axis=1) & np.isfinite(vectors).all(axis=1)
    assert ok.sum() > 50
    model = fit_ridge(vectors[ok], xy[ok], alpha=1.0, degree=1)
    split = contributions(model, vectors[ok]).sum(axis=1) + model.intercepts
    np.testing.assert_allclose(split, predict(model, vectors[ok]), rtol=1e-9, atol=1e-6)
