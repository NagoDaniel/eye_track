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
    assert not v.better and [r.key for r in v.worse] == [(7, "B", "a", "moving_dot")]


def test_a_worse_held_out_median_is_not_better() -> None:
    new = [_fake(7, "A", 150), _fake(7, "B", 100), _fake(7, "C", 280), _fake(6, "D", 160)]
    assert not verdict(BASE, new, "pause", "a", [7], [6]).better


def test_a_session_without_a_score_is_not_better() -> None:
    new = [_fake(7, "A", 150), _fake(7, "B", 90), _fake(6, "D", 140)]
    v = verdict(BASE, new, "pause", "a", [7], [6])
    assert not v.better and v.missing == [(7, "C", "a", "moving_dot")]


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
    imported = import_study(source, sessions_root(lab), a_session.study_id, assume_dot="moving_dot")
    assert [i.status for i in imported] == ["imported, dot videos ['moving_dot']"]
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
    result, report = run_experiment(exp, lab)
    assert result.failures == []  # B has no features_b here: skipped, not failed
    assert [(s.code, s.pipeline, s.video) for s in result.scores] == [
        (imported[0].session.code, "a", "moving_dot")
    ]
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


# --- E4 variants: feature columns and high-passed features ---


def test_the_running_baseline_removes_a_slow_drift_and_keeps_fast_changes() -> None:
    from lab.variants import running_baseline

    t = np.arange(0, 120, 1 / 30)
    slow = 0.002 * t  # posture drift
    fast = np.where((t // 1.5) % 2 == 0, 0.05, -0.05)  # 1.5 s gaze changes
    use = np.ones(len(t), dtype=bool)
    use[::7] = False  # skipped frames (blinks) are not part of the median
    x = slow + fast
    hp = x - running_baseline(t, x, use, window_s=20)
    inner = (t > 15) & (t < 105)
    np.testing.assert_allclose(hp[inner], fast[inner], atol=0.006)  # window edges cut a level


def test_add_highpass_appends_columns_and_keeps_rows_and_metadata(tmp_path: Path) -> None:
    from lab.variants import Highpass, add_highpass

    n = 300
    path = tmp_path / "features_a.parquet"
    table = pa.table({
        "pts": pa.array(np.arange(n) / 30.0), "face": pa.array([True] * n),
        "blink": pa.array([False] * n), "open_l": pa.array(np.linspace(0.3, 0.4, n)),
    }).replace_schema_metadata({"width": "1280"})  # fmt: skip
    pq.write_table(table, path)
    add_highpass(path, Highpass(("open_l",), window_s=2.0))
    out = pq.read_table(path)
    assert out.column_names == ["pts", "face", "blink", "open_l", "open_l_hp"]
    assert out.schema.metadata == {b"width": b"1280"} and out.num_rows == n
    hp = np.array(out["open_l_hp"].to_pylist())
    step = 0.1 / (n - 1)  # a window edge may take one sample more on one side
    assert np.abs(hp[60:-60]).max() <= step  # a straight line is its own centred mean


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"features": ["iris_l_x", "nose"]}, "unknown feature columns"),
        ({"features": ["open_l_hp"]}, "unknown feature columns"),  # no highpass table
        ({"features": ["iris_l_x", "iris_l_x"]}, "twice"),
        ({"features": []}, "at least one"),
        ({"highpass": {"columns": ["blink_l"], "window_s": 10}}, "highpass.columns"),
        ({"highpass": {"columns": ["open_l"], "window_s": 0}}, "window_s"),
        ({"highpass": {"columns": ["open_l"], "window": 10}}, "highpass must be"),
        ({"features": ["iris_l_x"], "pipelines": ["a", "b"]}, "Pipeline A only"),
    ],
)
def test_wrong_variants_are_refused(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ExperimentError, match=message):
        parse_experiment({"name": "x", "tune_studies": [7], **data})


def test_a_variant_experiment_parses() -> None:
    exp = parse_experiment({
        "name": "x", "tune_studies": [7], "features": ["iris_l_x", "open_l_hp", "tx"],
        "highpass": {"columns": ["open_l"], "window_s": 30},
    })  # fmt: skip
    assert exp.variant.feature_columns == ("iris_l_x", "open_l_hp", "tx")
    assert exp.variant.highpass is not None and exp.variant.highpass.window_s == 30.0


def test_a_variant_runs_on_the_real_fixture_with_its_own_features(
    a_session: FixtureSession, tmp_path: Path
) -> None:
    from worker.pipeline.features_a import FEATURES_FILE

    lab = tmp_path / "lab_data"
    import_study(a_session.root, sessions_root(lab), a_session.study_id, assume_dot="moving_dot")
    columns = ["iris_l_x", "iris_l_y", "iris_r_x", "iris_r_y", "open_l_hp", "open_r_hp", "tx"]
    exp = parse_experiment({
        "name": "variant", "tune_studies": [a_session.study_id], "features": columns,
        "highpass": {"columns": ["open_l", "open_r"], "window_s": 2},
    })  # fmt: skip
    result, report = run_experiment(exp, lab)
    assert result.failures == [] and len(result.scores) == 1
    derived = derived_dir(results_dir(lab, "variant") / "data", a_session.study_id,
                          a_session.session_id)  # fmt: skip
    calibration = json.loads((derived / SPEC_A.calibration_file).read_text())
    assert calibration["feature_columns"] == columns
    assert len(calibration["model"]["means"]) == len(columns)
    features = pq.read_table(derived / FEATURES_FILE)
    assert "open_l_hp" in features.column_names and "face_ratio_hp" not in features.column_names
    gaze = pq.read_table(derived / SPEC_A.gaze_file)
    assert np.isfinite(
        np.array([np.nan if v is None else v for v in gaze["x_px"].to_pylist()])
    ).any()
    header, _ = load_scores(lab, "variant")
    assert header["overrides"]["features"] == columns


# --- E7 (validation dots in the fit) and E6 (drift correction) ---


def _block(t_ms: float, gain: float, shift: tuple[float, float]) -> Any:
    """A validation block whose median predictions relate to the targets by a known map:
    prediction = (target - shift) / gain."""
    from worker.pipeline.validation import BlockScore, PointScore

    targets = [(640.0, 360.0), (320.0, 180.0), (960.0, 180.0), (960.0, 540.0), (320.0, 540.0)]
    points = [
        PointScore(
            i,
            x,
            y,
            t_ms,
            20,
            (x - shift[0]) / gain,
            (y - shift[1]) / gain,
            0.0,
            0.0,
            None,
            None,
            1.0,
        )  # fmt: skip
        for i, (x, y) in enumerate(targets)
    ]
    return BlockScore(0, "before_video", t_ms, points)


def test_the_affine_block_map_recovers_a_known_gain_and_shift() -> None:
    from lab.variants import block_map

    m = block_map(_block(1000.0, gain=0.8, shift=(50.0, -20.0)), "affine")
    assert m is not None
    assert m.gain == pytest.approx((0.8, 0.8)) and m.shift == pytest.approx((50.0, -20.0))
    off = block_map(_block(1000.0, gain=1.0, shift=(30.0, 10.0)), "offset")
    assert off is not None and off.gain == (1.0, 1.0) and off.shift == pytest.approx((30.0, 10.0))


def test_block_maps_blend_linearly_in_time_and_hold_outside() -> None:
    from lab.variants import BlockMap, correct

    maps = [BlockMap(0.0, (1.0, 1.0), (0.0, 0.0)), BlockMap(100.0, (1.0, 1.0), (10.0, -10.0))]
    t = np.array([-50.0, 0.0, 25.0, 100.0, 200.0])
    x, y = correct(maps, t, np.zeros(5), np.zeros(5))
    np.testing.assert_allclose(x, [0.0, 0.0, 2.5, 10.0, 10.0])
    np.testing.assert_allclose(y, [0.0, 0.0, -2.5, -10.0, -10.0])


@pytest.mark.parametrize(
    ("data", "message"),
    [({"fit_validation": "some"}, "fit_validation"), ({"drift": "spline"}, "drift"),
     ({"drift": "affine", "pipelines": ["a", "b"]}, "Pipeline A only")],
)  # fmt: skip
def test_wrong_fit_or_drift_modes_are_refused(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ExperimentError, match=message):
        parse_experiment({"name": "x", "tune_studies": [7], **data})


def test_offset_drift_rewrite_reproduces_the_products_columns_on_the_real_fixture(
    a_session: FixtureSession, tmp_path: Path
) -> None:
    """The lab's drift code in `offset` mode is the product's correction: same columns."""
    import shutil

    from lab.layout import SessionRef
    from lab.variants import rewrite_drift

    root = tmp_path / "data"
    shutil.copytree(a_session.root, root)
    derived = derived_dir(root, a_session.study_id, a_session.session_id)
    before = pq.read_table(derived / SPEC_A.gaze_file)
    ref = SessionRef(a_session.study_id, a_session.session_id, "X")
    rewrite_drift(root, ref, PipelineConfig(), SPEC_A, "offset")
    after = pq.read_table(derived / SPEC_A.gaze_file)
    assert after.column_names == before.column_names
    for name in ("x_px_dc", "y_px_dc", "x_sm_dc", "y_sm_dc", "x_vid_dc", "y_vid_dc"):
        a = np.array([np.nan if v is None else v for v in after[name].to_pylist()])
        b = np.array([np.nan if v is None else v for v in before[name].to_pylist()])
        assert np.isfinite(b).any(), name
        np.testing.assert_allclose(a, b, rtol=1e-6, atol=1e-4, err_msg=name)
    rewrite_drift(root, ref, PipelineConfig(), SPEC_A, "affine")  # runs on real data
    changed = pq.read_table(derived / SPEC_A.gaze_file)
    np.testing.assert_array_equal(  # the raw columns are untouched (NaN where unusable)
        changed["x_px"].to_numpy(), before["x_px"].to_numpy()
    )


def test_validation_dots_join_the_fit_on_the_real_fixture(
    a_session: FixtureSession, tmp_path: Path
) -> None:
    lab = tmp_path / "lab_data"
    import_study(a_session.root, sessions_root(lab), a_session.study_id, assume_dot="moving_dot")
    exp = parse_experiment({"name": "e7", "tune_studies": [a_session.study_id],
                            "fit_validation": "all", "drift": "affine"})  # fmt: skip
    result, _ = run_experiment(exp, lab)
    assert result.failures == [] and len(result.scores) == 1
    derived = derived_dir(results_dir(lab, "e7") / "data", a_session.study_id,
                          a_session.session_id)  # fmt: skip
    mine = json.loads((derived / SPEC_A.calibration_file).read_text())
    product = json.loads(
        (derived_dir(a_session.root, a_session.study_id, a_session.session_id)
         / SPEC_A.calibration_file).read_text()
    )  # fmt: skip
    assert len(mine["dots"]) > len(product["dots"])  # the validation dots were added
    assert mine["samples"]["valid"] > product["samples"]["valid"]
    header, _ = load_scores(lab, "e7")
    assert header["overrides"] == {"fit_validation": "all", "drift": "affine"}


# --- Several dot videos per session, the long recording videos, labels ---


def test_the_long_dot_videos_match_their_ground_truth() -> None:
    """Decoded frames of both long videos: the dark dot is where the path says (real ffmpeg via
    OpenCV), at rests, moves and pursuits."""
    import cv2

    from lab.dots import KNOWN, identify

    for name in ("long_dot_1", "long_dot_2"):
        video, path_file = KNOWN[name]
        assert identify(video) == name
        data = json.loads(path_file.read_text())
        assert 110 < data["duration_s"] < 120 and len(data["pauses"]) == 44
        assert [p["kind"] for p in data["pursuits"]] == ["figure_eight", "ellipse"]
        truth = DotTruth.load(path_file)
        assert len(truth.pursuits) == 2
        cap = cv2.VideoCapture(str(video))
        checked = 0
        for frame_no in range(0, len(data["samples"]), 97):
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_no)
            ok, frame = cap.read()
            assert ok, frame_no
            dark = frame.max(axis=2) < 60
            ys, xs = np.nonzero(dark)
            sample = data["samples"][frame_no]
            assert abs(xs.mean() - sample["x"]) < 2.0 and abs(ys.mean() - sample["y"]) < 2.0
            checked += 1
        cap.release()
        assert checked > 25


def test_every_dot_video_of_a_session_gets_its_own_score() -> None:
    """Two videos in one gaze table: each is scored against its own path and only its frames."""
    t1 = _gaze(offset_px=(10.0, 0.0))
    t2 = _gaze(offset_px=(0.0, 30.0))
    n = t1.num_rows
    t2 = t2.set_column(t2.schema.get_field_index("video_id"), "video_id", pa.array(["2"] * n))
    t2 = t2.set_column(t2.schema.get_field_index("t"), "t",
                       pa.array(np.array(t2["t"].to_pylist()) + 60_000))  # fmt: skip
    both = pa.concat_tables([t1, t2])
    s1 = score_gaze(both, _events(), TRUTH, PipelineConfig(), 7, "X", "a", "1", "moving_dot")
    s2 = score_gaze(both, _events(), TRUTH, PipelineConfig(), 7, "X", "a", "2", "moving_dot")
    assert s1.median == pytest.approx(SCALE * 10, rel=1e-6)
    assert s2.median == pytest.approx(SCALE * 30, rel=1e-6)
    assert s1.pursuit is None  # the 22 s video has no pursuit segment


def test_pursuit_error_is_measured_on_the_pursuit_segments_only() -> None:
    from lab.dots import truth

    long1 = truth("long_dot_1")
    stim = np.arange(0.0, long1.path.duration_s - 0.05, 1 / FPS)
    tx, ty = long1.path.at(stim)
    in_pursuit = np.zeros(len(stim), dtype=bool)
    for a, b in long1.pursuits:
        in_pursuit |= (stim >= a) & (stim <= b)
    x = tx + np.where(in_pursuit, 20.0 / long1.width, 0.0)  # 20 px off only while pursuing
    n = len(stim)
    table = pa.table({
        "t": pa.array(1000 + stim * 1000), "stim_time": pa.array(stim),
        "x_vid": pa.array(x), "y_vid": pa.array(ty), "x_vid_dc": pa.array(x),
        "y_vid_dc": pa.array(ty), "valid": pa.array([True] * n),
        "phase": pa.array(["playback"] * n), "video_id": pa.array(["5"] * n),
    })  # fmt: skip
    s = score_gaze(table, _events(), long1, PipelineConfig(), 8, "X", "a", "5", "long_dot_1")
    assert s.pursuit == pytest.approx(SCALE * 20, rel=1e-6)
    assert s.pause == pytest.approx(0, abs=1e-6) and s.video == "long_dot_1"
    assert len(s.rest_error) == 44


def test_old_scores_without_a_video_read_as_the_22_s_video() -> None:
    s = _score(_gaze())
    data = s.as_dict()
    del data["video"], data["pursuit"]
    old = Score.from_dict(data)
    assert old.video == "moving_dot" and old.pursuit is None


def test_labels_are_read_from_the_csv(tmp_path: Path) -> None:
    from lab.layout import read_labels

    assert read_labels(tmp_path) == {}
    (tmp_path / "labels.csv").write_text(
        "study,code,person,glasses,webcam,notes\n8,da0101,P1,no,built-in,evening\n"
        "8,DB0202,P1,yes,,\n"
    )
    assert read_labels(tmp_path) == {
        (8, "DA0101"): "person=P1 glasses=no webcam=built-in notes=evening",
        (8, "DB0202"): "person=P1 glasses=yes",
    }
    (tmp_path / "labels.csv").write_text("study,code,who\n")
    with pytest.raises(ValueError, match="header"):
        read_labels(tmp_path)


def test_fit_validation_before_and_between_is_a_known_mode() -> None:
    exp = parse_experiment({"name": "x", "tune_studies": [8],
                            "fit_validation": "before_and_between"})  # fmt: skip
    assert exp.variant.fit_validation == "before_and_between"


def test_the_time_plot_is_written_for_a_session_and_video(tmp_path: Path) -> None:
    from lab.plots import plot_video

    out = tmp_path / "p.png"
    plot_video(_gaze(offset_px=(20.0, -10.0)), TRUTH, "1", "title", "subtitle", out)
    assert out.is_file() and out.stat().st_size > 10_000
    assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
