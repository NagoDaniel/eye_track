"""Attentional synchrony on groups with known gaze (PLAN §9.4, §13)."""

import csv
from pathlib import Path

import numpy as np
import pytest

from worker.analysis.config import AnalysisConfig
from worker.analysis.density import StimSamples
from worker.analysis.synchrony import (
    NSS_FILE,
    SYNCHRONY_FILE,
    SYNCHRONY_PLOT,
    synchrony,
    write_synchrony,
)

CONFIG = AnalysisConfig()
SIGMA = 0.05


def staring(x: float, y: float, seconds: float = 1.0, start: float = 0.0, hz: int = 30,
            jitter: float = 0.0, seed: int = 0) -> StimSamples:  # fmt: skip
    t = np.arange(start, start + seconds, 1 / hz)
    rng = np.random.default_rng(seed)
    return StimSamples(t, x + rng.normal(0, jitter, len(t)), y + rng.normal(0, jitter, len(t)))


def run(people: list[StimSamples], duration: float = 1.0, aspect: float = 2.0):  # type: ignore[no-untyped-def]
    named = [(f"P{i + 1}", p) for i, p in enumerate(people)]
    return synchrony(named, duration, aspect, SIGMA, CONFIG)


def test_windows_are_200_ms_and_cover_the_video() -> None:
    r = run([staring(0.5, 0.5), staring(0.5, 0.5)], duration=1.0)
    np.testing.assert_allclose(r.window_start_s, [0, 0.2, 0.4, 0.6, 0.8])
    assert (
        len(run([staring(0.5, 0.5)] * 2, duration=1.1).window_start_s) == 6
    )  # the last is partial
    assert r.count.tolist() == [2] * 5


def test_identical_participants_have_no_dispersion() -> None:
    same = [staring(0.4, 0.6, jitter=0.0) for _ in range(3)]
    r = run(same)
    np.testing.assert_allclose(r.dispersion, 0.0, atol=1e-12)


def test_dispersion_is_the_mean_distance_to_the_group_median() -> None:
    # three at (0.5, 0.5), (0.5, 0.5), (0.8, 0.5): median (0.5, 0.5); distances 0, 0, 0.3
    r = run([staring(0.5, 0.5), staring(0.5, 0.5), staring(0.8, 0.5)])
    np.testing.assert_allclose(r.dispersion, 0.1)
    # two: median is the midpoint; each is 0.2 from it
    r = run([staring(0.3, 0.5), staring(0.7, 0.5)])
    np.testing.assert_allclose(r.dispersion, 0.2)


def test_vertical_distance_is_corrected_by_the_aspect_ratio() -> None:
    people = [staring(0.5, 0.3), staring(0.5, 0.7)]
    np.testing.assert_allclose(run(people, aspect=2.0).dispersion, 0.1)  # 0.2 high = 0.1 widths
    np.testing.assert_allclose(run(people, aspect=1.0).dispersion, 0.2)


def test_the_participant_median_is_used_not_every_sample() -> None:
    noisy = StimSamples(np.array([0.0, 0.05, 0.1]), np.array([0.5, 0.5, 0.9]), np.array([0.5] * 3))
    r = run([noisy, staring(0.5, 0.5)], duration=0.2)
    np.testing.assert_allclose(r.dispersion[0], 0.0)  # the median of the noisy one is 0.5


def test_a_window_with_fewer_than_two_participants_is_empty() -> None:
    a = staring(0.5, 0.5, seconds=1.0)
    b = staring(0.5, 0.5, seconds=0.4)  # looks only during the first two windows
    r = run([a, b])
    assert r.count.tolist() == [2, 2, 1, 1, 1]
    assert np.isfinite(r.dispersion[:2]).all() and np.isnan(r.dispersion[2:]).all()
    assert np.isnan(r.nss[2:]).all() and np.isnan(r.nss_mean[2:]).all()
    assert run([a]).count.tolist() == [1] * 5 and np.isnan(run([a]).dispersion).all()


def test_a_sample_at_the_edge_belongs_to_the_later_window() -> None:
    at_edge = StimSamples(np.array([0.2]), np.array([0.5]), np.array([0.5]))
    other = staring(0.5, 0.5, seconds=0.19, hz=100)
    r = run([at_edge, other], duration=0.4)
    assert r.count.tolist() == [
        1,
        1,
    ]  # the edge sample is in the second window, the other's in the first


def test_nss_is_high_where_the_others_look_and_negative_for_an_outlier() -> None:
    cluster = [staring(0.3, 0.3, jitter=0.01, seed=s) for s in range(3)]
    outlier = staring(0.8, 0.8, jitter=0.01, seed=9)
    r = run([*cluster, outlier])
    assert (r.nss[:, :3] > 1.0).all()  # the others include two cluster mates and the outlier
    assert (r.nss[:, 3] < 0).all()
    summary = r.summary()
    nss = summary["nss_by_participant"]
    assert isinstance(nss, dict) and nss["P1"] > 1 and nss["P4"] < 0


def test_two_who_look_at_the_same_place_agree_more_than_two_who_do_not() -> None:
    together = run([staring(0.5, 0.5, jitter=0.01, seed=1), staring(0.5, 0.5, jitter=0.01, seed=2)])
    apart = run([staring(0.2, 0.2, jitter=0.01, seed=1), staring(0.8, 0.8, jitter=0.01, seed=2)])
    assert np.nanmean(together.nss) > 0 > np.nanmean(apart.nss)
    assert np.nanmean(together.dispersion) < np.nanmean(apart.dispersion)


def test_summary_counts_windows() -> None:
    s = run([staring(0.5, 0.5, seconds=1.0), staring(0.5, 0.5, seconds=0.4)]).summary()
    assert s["windows"] == 5 and s["windows_with_two_or_more"] == 2


def test_files_hold_the_series_and_a_png(tmp_path: Path) -> None:
    r = run([staring(0.3, 0.5), staring(0.7, 0.5, seconds=0.6)])
    assert write_synchrony(tmp_path, r) == [SYNCHRONY_FILE, NSS_FILE, SYNCHRONY_PLOT]
    with (tmp_path / SYNCHRONY_FILE).open() as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 5 and rows[0]["participants"] == "2"
    assert float(rows[0]["dispersion"]) == pytest.approx(0.2)
    assert rows[4]["participants"] == "1" and rows[4]["dispersion"] == ""
    assert float(rows[1]["window_end_s"]) == pytest.approx(0.4)
    with (tmp_path / NSS_FILE).open() as f:
        per = list(csv.DictReader(f))
    assert len(per) == 5 * 2 and per[0]["participant_code"] == "P1"
    assert (tmp_path / SYNCHRONY_PLOT).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert not list(tmp_path.glob("*.tmp"))


def test_no_participants_still_gives_a_file(tmp_path: Path) -> None:
    r = synchrony([], 1.0, 1.0, SIGMA, CONFIG)
    assert (r.count == 0).all()
    write_synchrony(tmp_path, r)
    assert (tmp_path / SYNCHRONY_PLOT).stat().st_size > 1000
