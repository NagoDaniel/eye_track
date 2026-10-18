"""The calibration's ridge regression (PLAN §8.6) against scikit-learn and the browser's fixture."""

import json
from pathlib import Path
from typing import Literal

import numpy as np
import pytest
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler

from worker.pipeline.ridge import RidgeModel, fit_ridge, predict

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "shared" / "fixtures" / "ridge_fit.json").read_text()
)


def sklearn_predictions(
    x: np.ndarray, y: np.ndarray, new_x: np.ndarray, alpha: float, degree: Literal[1, 2]
) -> np.ndarray:
    out = np.zeros((len(new_x), 2))
    for column in range(2):
        steps = [StandardScaler()]
        if degree == 2:
            steps.append(PolynomialFeatures(degree=2, include_bias=False))
        model = make_pipeline(*steps, Ridge(alpha=alpha, fit_intercept=True))
        model.fit(x, y[:, column])
        out[:, column] = model.predict(new_x)
    return out


@pytest.mark.parametrize("degree,alpha", [(2, 1.0), (1, 10.0), (2, 0.001), (2, 1000.0)])
def test_matches_scikit_learn(degree: Literal[1, 2], alpha: float) -> None:
    x = np.array(FIXTURE["features"])
    y = np.array(FIXTURE["targets"])
    new_x = np.array(FIXTURE["new_features"])
    ours = predict(fit_ridge(x, y, alpha, degree), new_x)
    np.testing.assert_allclose(
        ours, sklearn_predictions(x, y, new_x, alpha, degree), rtol=1e-6, atol=1e-5
    )


def test_matches_the_numbers_the_browser_version_is_tested_with() -> None:
    x, y = np.array(FIXTURE["features"]), np.array(FIXTURE["targets"])
    new_x = np.array(FIXTURE["new_features"])
    quadratic = predict(fit_ridge(x, y, FIXTURE["alpha"], 2), new_x)
    np.testing.assert_allclose(quadratic, np.array(FIXTURE["predictions"]), rtol=1e-6, atol=1e-6)
    linear = predict(fit_ridge(x, y, FIXTURE["linear_alpha"], 1), new_x)
    np.testing.assert_allclose(
        linear, np.array(FIXTURE["linear_predictions"]), rtol=1e-6, atol=1e-6
    )


def test_a_constant_column_is_harmless() -> None:
    x = np.array(FIXTURE["features"])  # column 3 is constant
    assert np.ptp(x[:, 3]) == 0
    model = fit_ridge(x, np.array(FIXTURE["targets"]), 1.0, 2)
    assert model.stds[3] == 1.0 and np.isfinite(model.weights).all()


def test_a_model_survives_json() -> None:
    x, y = np.array(FIXTURE["features"]), np.array(FIXTURE["targets"])
    model = fit_ridge(x, y, 1.0, 2)
    again = RidgeModel.from_dict(json.loads(json.dumps(model.as_dict())))
    np.testing.assert_array_equal(predict(model, x), predict(again, x))


def test_bad_input_fails_loudly() -> None:
    x = np.ones((5, 3))
    y = np.zeros((5, 2))
    with pytest.raises(ValueError, match="alpha"):
        fit_ridge(x, y, 0.0, 1)
    with pytest.raises(ValueError, match="at least"):
        fit_ridge(x[:1], y[:1], 1.0, 1)
    with pytest.raises(ValueError, match="finite"):
        fit_ridge(np.full((5, 3), np.nan), y, 1.0, 1)
    with pytest.raises(ValueError, match="targets"):
        fit_ridge(x, np.zeros((4, 2)), 1.0, 1)
    with pytest.raises(ValueError, match="takes 3 features"):
        predict(fit_ridge(x * np.arange(5)[:, None], y, 1.0, 1), np.ones((2, 4)))
