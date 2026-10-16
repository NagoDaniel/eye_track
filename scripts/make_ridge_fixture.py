"""Write shared/fixtures/ridge_fit.json: scikit-learn reference numbers for frontend/src/face/ridge.ts.

The browser fits the calibration gate with a hand-written ridge (PLAN §5.6 step 3); this fixture holds
what scikit-learn gives for the same pipeline, so the TypeScript test can compare the two (within 1e-6).

Run from backend/ (numpy and scikit-learn are not project dependencies):
    UV_PROJECT_ENVIRONMENT=<main repo>/backend/.venv uv run --with scikit-learn python ../scripts/make_ridge_fixture.py
"""

import json
from pathlib import Path

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler

SEED = 20240611
SAMPLES = 120
FEATURES = 10  # the Pipeline A vector of quickCalib.ts
NEW_SAMPLES = 25
ALPHA = 1.0
LINEAR_ALPHA = 10.0  # a different alpha, so the test sees that alpha and degree are both used
CONSTANT_COLUMN = 3  # StandardScaler turns a zero std into 1
OUT = Path(__file__).resolve().parent.parent / "shared" / "fixtures" / "ridge_fit.json"


def main() -> None:
    rng = np.random.default_rng(SEED)
    # Different scale and offset per column, one constant column.
    scales = rng.uniform(0.1, 30.0, FEATURES)
    offsets = rng.uniform(-5.0, 5.0, FEATURES)
    x = rng.normal(size=(SAMPLES, FEATURES)) * scales + offsets
    x[:, CONSTANT_COLUMN] = 2.5
    new_x = rng.normal(size=(NEW_SAMPLES, FEATURES)) * scales + offsets
    new_x[:, CONSTANT_COLUMN] = 2.5
    # Noisy nonlinear targets in "pixels".
    z = (x - offsets) / scales
    y = np.column_stack(
        [
            960 + 300 * z[:, 0] - 120 * z[:, 1] * z[:, 2] + 80 * z[:, 4] ** 2,
            540 - 200 * z[:, 1] + 90 * z[:, 0] * z[:, 5] + 50 * z[:, 6] ** 2,
        ]
    ) + rng.normal(scale=5.0, size=(SAMPLES, 2))

    predictions = np.zeros((NEW_SAMPLES, 2))
    for column in range(2):
        model = make_pipeline(
            StandardScaler(),
            PolynomialFeatures(degree=2, include_bias=False),
            Ridge(alpha=ALPHA, fit_intercept=True),
        )
        model.fit(x, y[:, column])
        predictions[:, column] = model.predict(new_x)

    linear_predictions = np.zeros((NEW_SAMPLES, 2))
    for column in range(2):
        model = make_pipeline(StandardScaler(), Ridge(alpha=LINEAR_ALPHA, fit_intercept=True))
        model.fit(x, y[:, column])
        linear_predictions[:, column] = model.predict(new_x)

    payload = {
        "comment": "scikit-learn reference for ridge.ts; regenerate with scripts/make_ridge_fixture.py",
        "alpha": ALPHA,
        "features": x.tolist(),
        "targets": y.tolist(),
        "new_features": new_x.tolist(),
        "predictions": predictions.tolist(),
        "linear_alpha": LINEAR_ALPHA,
        "linear_predictions": linear_predictions.tolist(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload) + "\n")
    print(f"wrote {OUT} ({SAMPLES} samples, {NEW_SAMPLES} predictions)")


if __name__ == "__main__":
    main()
