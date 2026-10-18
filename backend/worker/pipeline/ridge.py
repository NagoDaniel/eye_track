"""Closed-form ridge regression for the calibration (PLAN §8.6), the Python twin of
`frontend/src/face/ridge.ts`.

The model is scikit-learn's
    make_pipeline(StandardScaler(), PolynomialFeatures(degree, include_bias=False),
                  Ridge(alpha, fit_intercept=True))
fitted for the x and y targets at once. Degree 1 leaves out the polynomial step. The tests compare
it with scikit-learn and with the numbers the browser's version is tested against
(`shared/fixtures/ridge_fit.json`).
"""

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

Floats = NDArray[np.float64]
Degree = Literal[1, 2]
MIN_SAMPLES = 2


@dataclass(frozen=True)
class RidgeModel:
    alpha: float
    degree: Degree
    means: Floats  # per input column
    stds: Floats  # per input column, population std; 0 is replaced by 1
    weights: Floats  # (expanded columns, 2): x and y
    intercepts: Floats  # (2,)

    def as_dict(self) -> dict[str, Any]:
        return {
            "alpha": self.alpha,
            "degree": self.degree,
            "means": self.means.tolist(),
            "stds": self.stds.tolist(),
            "weights": [self.weights[:, 0].tolist(), self.weights[:, 1].tolist()],
            "intercepts": self.intercepts.tolist(),
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "RidgeModel":
        weights = np.array(data["weights"], dtype=np.float64).T
        return RidgeModel(
            alpha=float(data["alpha"]),
            degree=2 if data["degree"] == 2 else 1,
            means=np.array(data["means"], dtype=np.float64),
            stds=np.array(data["stds"], dtype=np.float64),
            weights=weights,
            intercepts=np.array(data["intercepts"], dtype=np.float64),
        )


def expand(rows: Floats, degree: Degree) -> Floats:
    """The original columns, then for degree 2 x_i * x_j for i <= j (scikit-learn's order)."""
    if degree == 1:
        return rows
    i, j = np.triu_indices(rows.shape[1])
    return np.concatenate([rows, rows[:, i] * rows[:, j]], axis=1)


def fit_ridge(features: Floats, targets: Floats, alpha: float, degree: Degree) -> RidgeModel:
    """Fit on `features` (samples, columns) and `targets` (samples, 2: x and y in px)."""
    if not alpha > 0:
        raise ValueError("alpha must be positive")
    if features.ndim != 2 or targets.shape != (len(features), 2):
        raise ValueError("features must be (samples, columns) and targets (samples, 2)")
    if len(features) < MIN_SAMPLES:
        raise ValueError(f"need at least {MIN_SAMPLES} samples, got {len(features)}")
    if not (np.isfinite(features).all() and np.isfinite(targets).all()):
        raise ValueError("features and targets must be finite")
    means = features.mean(axis=0)
    stds = features.std(axis=0)
    stds[stds == 0] = 1.0
    expanded = expand((features - means) / stds, degree)
    expanded_means = expanded.mean(axis=0)
    centred = expanded - expanded_means
    target_means = targets.mean(axis=0)
    gram = centred.T @ centred + alpha * np.eye(centred.shape[1])
    weights = np.linalg.solve(gram, centred.T @ (targets - target_means))
    return RidgeModel(
        alpha=alpha,
        degree=degree,
        means=means,
        stds=stds,
        weights=weights,
        intercepts=target_means - expanded_means @ weights,
    )


def predict(model: RidgeModel, features: Floats) -> Floats:
    """Predicted (x, y) for each row, shape (samples, 2)."""
    if features.ndim != 2 or features.shape[1] != len(model.means):
        raise ValueError(f"the model takes {len(model.means)} features per row")
    expanded = expand((features - model.means) / model.stds, model.degree)
    return np.asarray(expanded @ model.weights + model.intercepts, dtype=np.float64)
