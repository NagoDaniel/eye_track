# Experiment lab (T13)

Try changes to the gaze estimate on recorded moving-dot sessions in seconds, without the app.
The lab runs the **product's own stage functions** (features, sync, calibration, gaze, the
moving-dot report) on copies of sessions, with a `PipelineConfig` from an experiment file, and
compares the result with the baseline. Not part of the product; never deployed
(DECISIONS 2026-10-30). Background: `docs/PIPELINE_OVERVIEW.md`.

Runs natively (backend venv: `cd backend && uv sync`), from the repository root:

```
make lab ARGS="import-study 7"            # copy study 7 from data/ into lab_data/ (data/ is only read)
make lab ARGS="import-study 6"
make lab ARGS="reference 7 6"             # score the product's outputs as they are in data/
make lab ARGS="run baseline"              # re-run the pipeline in the lab (about 5 s for 10 sessions)
make lab ARGS="run my_experiment"         # backend/lab/experiments/my_experiment.toml
make lab ARGS="compare my_experiment"     # session by session against baseline, with the verdict
```

Results: `lab_data/results/<experiment>/report.txt` and `scores.json`; the experiment's gaze tables
and calibrations are under `lab_data/results/<experiment>/data/` (same layout as `data/`).

## An experiment

A TOML file in `backend/lab/experiments/` (copy `baseline.toml`):

```toml
name = "alpha_grid_fine"
description = "finer ridge alpha grid"
pipelines = ["a"]
tune_studies = [7]       # parameters may be chosen on these
holdout_studies = [6]    # only checks the choice

[config]                 # any PipelineConfig field (backend/worker/pipeline/config.py)
calib_alphas = [0.3, 1, 3, 10, 30, 100]
```

Pipeline A's regression input and derived features (`lab/variants.py`; Pipeline A only):

```toml
features = ["iris_l_x", "iris_l_y", "iris_r_x", "iris_r_y", "open_l_hp", "open_r_hp",
            "yaw", "pitch", "roll"]   # any column of features_a.parquet, or a high-passed one

[highpass]                            # adds <column>_hp = column minus its running trimmed mean
columns = ["open_l", "open_r"]
window_s = 60
```

```toml
fit_validation = "all"   # E7: also fit the calibration on validation dots: none, before_video, all
drift = "affine"         # E6: drift correction: offset (the product's) or affine (per block)
```

Unknown keys, unknown fields, unknown columns and wrong types are refused.

`make lab ARGS="diagnose [experiment]"` (E1): head pose against the calibration spread, what each
feature group contributes to the drift, frame stamping, and a ridge-alpha sweep.

## Reading the numbers

- **Table 1** (`moving_dot_report`): error in **viewport px** (the participant's screen in CSS px).
  `scale` is viewport px per video px (1.50 in study 7, 1.34 in study 6).
- **Tables 2 to 4**: **video px** (1280 x 720), as in the old `dot_eval.py`.
- `pause`: median error while the dot rests (accuracy without lag). `dc_`: drift-corrected (what the
  analyses use).
- **Verdict** (DECISIONS 2026-10-30): better when the median over the tuning sessions falls, no
  session gets worse by more than 10 %, and the held-out median does not get worse. Shown for
  `dc_pause` and `pause`.
- All sessions so far are one person: a result holds for that person only.

## Rules

- `data/` is only read. The lab's raw copies are read-only.
- `lab_data/` is gitignored (face recordings): never commit it.
- A variant the product cannot express yet (other features, models, drift corrections) gets a small
  seam in the product code: its own commit, same behaviour for the product, a DECISIONS entry.
- macOS prints `objc[...]: Class AVFFrameReceiver is implemented in both ...` (PyAV and OpenCV
  bundle the same library). Harmless here; the lab does not use the camera through either yet.
