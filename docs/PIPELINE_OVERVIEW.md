# How the gaze estimate is made: an overview for whoever tunes it

This page explains the whole chain from the participant's webcam to a gaze point on the video, what each model is for, what is stored, how well it works so far (with measured numbers), and where improvements could come from. It is written for a developer or agent who has to understand the system before changing its accuracy. The specification is `docs/PLAN.md` (never edit it); every deviation is in `docs/DECISIONS.md`; the schema of every file is in `docs/DATA_FORMATS.md`. This page is the map between them. Where this page and the code disagree, the code is right: fix this page.

## 1. The chain in one picture

```
PARTICIPANT'S BROWSER                                  SERVER (worker, offline)
────────────────────                                   ─────────────────────────
webcam 1280x720 @30 ──┬─► MediaRecorder (VP9 WebM, 2.5 Mbps, 2 s chunks) ─┐
                      │                                                   │ upload (chunked, resumable)
                      └─► MediaPipe Face Landmarker (WASM, web worker)    │
                           landmarks + head-pose matrix + blendshapes     │
                           ──► features stream (JSON lines) ──────────────┤
stimulus video ──► requestVideoFrameCallback ──► stimulus_frames stream ──┤
all UI events (dots, clicks, video, fullscreen…) ──► events stream ───────┘
                                                                          ▼
 calibration: 13 dots   validation: 5 dots (before, between, after videos)
                                                                          ▼
 ingest ─► landmarks (MediaPipe again, on the recorded video, every frame, full resolution)
        ─► features_a (10 numbers per frame: iris position, eye openness, head angles, face size)
        ─► sync (align webcam time with the browser clock)
        ─► calibration (per-participant ridge regression: features ─► screen px, from the 13 dots)
        ─► gaze (predict, validity, Kalman smoothing, drift correction from validation dots,
                 mapping to video time and video coordinates, fixations)
        ─► quality (report, inclusion decision) ─► plots
 optional, separate job: Pipeline B = the same, but the eye signal comes from a neural network (L2CS)
                                                                          ▼
 analysis: heatmaps, AOI metrics, attentional synchrony; comparison of A and B
```

Two ideas carry everything:

1. **Every participant is calibrated on their own, in the session.** There is no model of "where people look in general". For each session a small regression is fitted from the 13 calibration dots (known screen position, known eye features at that moment) and applied to every webcam frame of the same session. All accuracy limits come from how well the 10 numbers per frame describe where the eyes point, and how stable the person's head and posture stay after the calibration.
2. **All the hard work is offline.** The browser only records and runs a quick face model for a calibration quality check; the authoritative gaze is computed on the server from the recorded video, at full resolution, on every frame.

## 2. In the browser (`frontend/src`)

**Recording.** `media/camera.ts` asks for 1280x720 at 30 fps (study config `recording`), accepts down to 640x480. `MediaRecorder` writes WebM (VP9 preferred, then VP8) at about 2.5 Mbit/s, one chunk per 2 s. The recording starts at the fullscreen step and ends after the last validation. Chunks of one recording are one WebM byte stream. This compressed video is what the server decodes later, so VP9 compression noise is part of the landmark noise.

**Live face model (the same Google model as on the server).** `face/modelWorker.ts` runs **MediaPipe Face Landmarker** (tasks-vision, WASM, **CPU** delegate, VIDEO mode, blendshapes and facial transformation matrix on, `face_landmarker.task` float16 v1; URL and hash in `shared/models.json`) in a Web Worker. It produces 478 landmarks (468 face mesh + 10 iris points), 52 blendshape scores (only `eyeBlinkLeft/Right` are kept) and a 4x4 head-pose matrix. It looks for up to 2 faces (the camera check refuses a second person). Its rate is `face/adaptiveRate.ts`: the video's frame rate capped by the camera's, **lowered** by 25 % steps (floor 8 fps) when more than 5 % of the stimulus video's frames are dropped in a 2 s window, and raised again after 5 s without drops. Playback smoothness has priority over tracking rate.

**What is sent from the face model** (`features` stream, `face/featureRecord.ts`): per processed frame `{t, face, lm (a fixed subset of 49 landmarks, shared/landmark_subset.json), matrix (16 numbers, row-major, translation in cm), blink_l, blink_r, face_ratio, brightness, ...}`. It has three uses: (1) the calibration quality gate in the browser, (2) the live debug gaze dot, (3) **fine time alignment** of the recorded video (sync, below). It is *not* used for the final gaze.

**Calibration gate (`face/calibrationGate.ts`, `face/quickCalib.ts`).** Right after the first validation block the browser fits a quick model with the same method as the server (below) on the calibration dots, scores it on the 5 validation dots, and passes the session when the mean error is at most 0.10 of the viewport diagonal and no point above 0.18. If not, the participant sees a short message and repeats the calibration (max 3 attempts, then `continue_flagged`). The server uses the **last attempt that passed** (else the last attempt).

**Calibration and validation screens** (`calibration/`, `screens/`): 13 dots (3x3 grid plus 4 quadrant centres, inset 6 % of the viewport, random order starting at the centre); a dot shrinks over `dot_duration_ms` (1500) and the participant clicks it in the last 600 ms; the training window of a dot is **700 ms before to 100 ms after the click** (the saccade to the dot is excluded). Validation: 5 dots (centre and the four points at 25 %/75 %), same behaviour, **never used for fitting**, only for measuring. Validation blocks happen before the first video, between videos, and after the last one (config `validation`).

**Stimulus frames.** `requestVideoFrameCallback` on the stimulus `<video>` logs, for every frame the browser presented, `{t, expected_display_time, media_time, presented_frames}`. The server maps client time to video time with this stream (stimulus time is `null` during pauses, stalls, between videos). The letterboxed content rectangle of the video in viewport px is logged in `viewport_change` events.

**Events.** Everything that happens (dots shown/clicked, video load/play/ended/stall, fullscreen changes, window blur, face lost/regained, gate results, `recorder_start` with the camera settings, client info with user agent/screen/cores) goes to the `events` stream on the same clock `t` = `performance.now()`.

## 3. What reaches the server (raw, immutable)

`raw/` of a session (`studies/<study>/sessions/<uuid>/raw/`): `webcam.webm` (assembled and later remuxed), `events.jsonl`, `browser_features.jsonl`, `stimulus_frames.jsonl`, `meta.json` (the study config snapshot at the time and flags). Raw data is never changed by the pipeline. Everything below is in `derived/` and can be recomputed from `raw/` by `cli.py process`.

## 4. Pipeline A on the server (`backend/worker/pipeline/`)

Stages in order (`runner.py`): `ingest`, `landmarks`, `features`, `sync`, `calibration`, `gaze`, `quality`, `plots`. Each reads and writes files, is idempotent, and records its duration and a summary in `stages.json`. All thresholds are named fields of `PipelineConfig` (`config.py`), written into the output files.

1. **ingest** (`ingest.py`): checks all chunks (seq complete, sha256), remuxes the WebM with ffmpeg (`-c copy -fflags +genpts`) so it has a duration and seek index, extracts per-frame presentation times (`webcam_frames.parquet`). The frame rate is variable; nothing assumes constant fps.
2. **landmarks** (`landmarks.py`): MediaPipe Face Landmarker (Python, VIDEO mode, same model file) on **every decoded frame at full resolution**, one face max. Output `landmarks.parquet`: 478 landmarks x/y/z (float32), the 16-number head-pose matrix and the two blink scores (`eyeBlinkLeft/Right`; the other blendshapes are not stored), per frame; frames without a face are marked.
3. **features** (`features_a.py`, its twin `frontend/src/face/features.ts`; the two must agree within 1e-4, tested on `shared/fixtures/face_frames.json`): per frame, for each eye, in an **eye-local frame** (origin at the midpoint of the two eye corners, x along the corner-to-corner vector, y a quarter turn towards the image bottom, both scaled by the eye width, landmarks first scaled by the frame aspect ratio): the **iris centre** (mean of the 5 iris landmarks), x and y; the **eye openness** (lid distance / eye width). Plus head **yaw, pitch, roll** (degrees, from the transformation matrix, `R = Rz Ry Rx`), the translation `tx, ty, tz` (cm, not used by the regression), and `face_ratio` (distance between the eye midpoints / frame width). `blink` = either `eyeBlink` blendshape > 0.5 or openness < 0.15. The **feature vector** of the regression is these ten: `iris_l_x, iris_l_y, iris_r_x, iris_r_y, open_l, open_r, yaw, pitch, roll, face_ratio`. A frame without a face has null features and is not valid.
4. **sync** (`sync.py`): the browser's feature stream and the server's landmarks describe the same video; a per-frame signal (horizontal iris position relative to the eye-corner midpoint, plus head yaw, z-scored) is cross-correlated at 100 Hz within +-1000 ms of the coarse offset `recorder_start + pts`; the same in 60 s windows, then a robust (Huber) line `t = a + b * pts` is fitted (offset and clock drift). Writes `sync.json` with flags (`sync_short` for recordings under 2 minutes is normal). Result: every webcam frame has a time on the browser's clock `t`.
5. **calibration** (`calibration.py`, `ridge.py`): from the events, which dots were shown where and when they were clicked (per attempt). Samples = usable frames (face, no blink, finite features) in each dot's training window; target = dot position in viewport px. Model = scikit-learn style `StandardScaler -> (PolynomialFeatures degree 2) -> Ridge(alpha)` for x and y at once. **Leave-one-dot-out cross-validation** chooses alpha from 1e-3...1e3 (one value per decade) for both the degree-2 and the plain linear model; the one with the lower CV error is kept (ties: simpler, stronger regularisation). Outliers (further than 2.5 x the median distance from their dot's median prediction) are dropped and the model is chosen and fitted again. The calibration fails if more than 3 dots have fewer than 5 usable frames. Output `calibration_a.json` (parameters, both CV errors, chosen model, attempts).
6. **gaze** (`gaze.py`, `predict.py`, `smoothing.py`, `validation.py`, `fixations.py`, `stimulus_map.py`):
   - **predict**: apply the model to every usable frame -> `x_px, y_px` in viewport px. `valid` = usable frame and prediction within the viewport expanded by 15 %.
   - **smooth**: invalid gaps up to 150 ms are linearly interpolated (flagged `interpolated`, `valid` stays false); then a Rauch-Tung-Striebel (forward-backward) **Kalman smoother**, constant-velocity model, measurement noise 15 px, process noise 3e7 px^2/s^3 (tuned on synthetic data so a 300 px step settles within about 60 ms). Longer gaps split the smoother into segments. Columns `x_sm, y_sm`.
   - **validation and drift**: for each validation block, per point: accuracy = distance of the **median prediction** (valid frames in the same window as the training window) to the target, precision = RMS sample-to-sample step. The block's **offset vector** is the mean over its points of (median prediction minus target). Between blocks the offset is linearly interpolated in time (constant before the first and after the last block) and subtracted: the `*_dc` ("drift-corrected") columns. Both versions are kept; **the analysis uses the drift-corrected one by default** (DECISIONS 2026-10-20).
   - **time and video coordinates**: each frame gets a `phase` (calibration/validation/playback/other), the video id and `stim_time` (from `stimulus_frames`, with pauses, stalls and seeks as gaps), and the gaze in **video-normalised coordinates** `x_vid, y_vid` (fractions of the letterboxed video rectangle at that moment).
   - **fixations**: I-DT on smoothed valid playback samples, dispersion threshold `max(0.05 x viewport width, 2 x session validation precision)`, min duration 100 ms.
7. **quality** (`quality.py`): valid fraction, webcam rate and gaps, face-lost episodes, validation accuracy/precision per block, drift, CV error, sync flags, **dropped stimulus frames** (flag above 10 %), inclusion decision against the study's `inclusion` thresholds (`min_valid_fraction` 0.7, `max_validation_error_norm` 0.12 of the viewport diagonal). `quality.json`.
8. **plots** (`plots.py`): `derived/debug/*.png` (calibration, validation, gaze, sync, signals).

Files and column lists of every stage: `docs/DATA_FORMATS.md`, "Derived files of the pipelines".

## 5. Pipeline B (the gaze network) (`features_b.py`, `model_b.py`, `runner_b.py`)

Same chain from `calibration` on (same `ridge.py`, same predict/smooth/validate/fixations code, a `PipelineSpec` names the files and the feature columns). What differs is only the **eye signal**:

- Model: **L2CS-Net** (Gaze360-trained, ONNX, via onnxruntime; ResNet-50 is the default, MobileOne-s0 is the small one used in tests; weights from `./scripts/fetch_models.sh`, hashes in `shared/models.json`; research-licence, not committed). Why: L2CS is a published appearance-based gaze network that predicts **gaze angles (yaw and pitch) from a face crop**, so it needs no hand-made iris geometry and can in principle cope with head poses that the iris geometry does not model. The plan wanted a second, independent estimate to compare with A.
- Input: a **square crop** around the centre of the landmark bounding box (longer side x 1.2), resized to 448x448, RGB, 0-1, ImageNet mean/std. Output: two 90-bin logits (yaw, pitch); the angle is `softmax -> expected bin * 4 - 180` degrees, converted to radians. Both weight files have a fixed batch of 1, so frames go through the network one by one (`b_batch_size` is ignored).
- Features B (6): `gaze_pitch, gaze_yaw` (network) + `yaw, pitch, roll, face_ratio` (head pose and size from A). Blinks and validity are A's. The per-session ridge then maps these six numbers to screen px.
- Cost: about 0.07 s per face on a desktop CPU (about twice the recording length per session). It runs as its own job (`process_pipeline_b`), after every other job, and writes its own files (`features_b`, `calibration_b.json`, `gaze_b`, `fixations_b`, `quality_b.json`, `stages_b.json`). It is marked **stale** when A is re-run from `ingest`, `landmarks`, `features` or `sync` (or when a B run stops part-way).

## 6. After the gaze: analysis (`backend/worker/analysis/`)

Per video across the included sessions: equal-weight Gaussian density heatmaps (64x36 grid; dynamic video and static pictures per segment), AOI metrics (dwell by fixations and by samples, time to first fixation, revisits; AOIs are rectangles/polygons in video coordinates with start and end times, optional tolerance = k x the participant's own accuracy), attentional synchrony (dispersion and NSS per 200 ms window), and `compare_pipelines` (A against B per session, paired Wilcoxon, from 6 pairs). `run.json` records parameters and the code version. Not needed to tune the gaze, but the AOI problem (below) is a consequence of the gaze accuracy.

## 7. Models and libraries, and why

| What | Where | Why |
|---|---|---|
| MediaPipe Face Landmarker (float16) | browser (WASM, CPU) and server (Python) | one model gives the face mesh, **iris landmarks**, blink scores and the head-pose matrix, in real time on a laptop CPU, no training; running the same model on both sides lets the browser features serve as a cross-check and for time sync |
| Ridge regression (degree 1 or 2), per session | server, `ridge.py`; browser, `ridge.ts` | the only supervised part; tiny, closed-form, per participant, cross-validated over dots; the same closed form in TypeScript gives the browser gate |
| Rauch-Tung-Striebel Kalman smoother | `smoothing.py` | offline, so forward-backward is allowed: noise is cut without lag |
| I-DT | `fixations.py` | standard, simple, parameters from the validation precision |
| L2CS-Net (ONNX) | Pipeline B | an independent appearance-based estimate for comparison |
| ffmpeg / PyAV | ingest, replay | remux, decode frames |
| Parquet (pyarrow), JSON lines | storage | formats in DATA_FORMATS |

## 8. How good it is, measured

Ground truth: the **moving-dot video** (`e2e/fixtures/moving_dot.mp4`, 21.8 s, 1280x720, 25 fps, 10 rest positions of 1.5 s joined by 0.7 s moves; path in `moving_dot_path.json`). A participant follows the dot with their eyes, so the dot position is the true gaze target at every video time. `cli.py moving-dot-report <session>` and `docs/evidence/m6-tuning-baseline/dot_eval.py` score a processed session against it. Sources of the numbers: PROGRESS "H1 result", "H2 result", "H3 result".

Baseline of the current system (**study 7, six sessions, second webcam**; full output in `docs/evidence/m6-tuning-baseline/baseline_study7.txt`; study 6, four sessions, in `baseline_study6.txt`). Median distance between gaze and dot in **viewport px** (CSS px of the participant's screen; the video filled 1.50 viewport px per video px in study 7, 1.34 in study 6, so these are not px of the 1280x720 video, as earlier versions of this page said), as measured (A, B); 0202 = glasses, 0101 = none; all sessions are of one person:

| session | A | B |
|---|---|---|
| GG0101 | 221 | 231 |
| DD0202 | 165 | 216 |
| EE0202 | 162 | 212 |
| AA0101 | 286 | 385 |
| BB0202 | 260 | 335 |
| FF0101 | 237 | 179 |

Study 6 (A): 199, 257, 249 and 255 px as measured; 181, 260, 174 and 136 px drift-corrected (two with glasses).

What the numbers show (details in `baseline_study7.txt` and PROGRESS "H3 result"):

- **The error is mostly systematic, not noise.** Within one rest position the spread is 30 to 76 px; the median distance is 140 to 290 px.
- **Gaze is pulled towards the centre** (regression slope of gaze on dot 0.6 to 0.9; at the right-hand corners A falls 70 to 120 px short, B 210 to 240 px) and **vertical is the weak axis** (correlation with the dot 0.68 to 0.91, horizontal 0.93 to 0.98).
- **The error changes over time:** at the last rest position, the same centre as the first, gaze is 90 to 170 px above the dot, against about 20 px at the start. A constant gain and offset fitted on the first half of the video does not help on the second half **in study 7** (table 4), but it does in **study 6** (second-half error in video px 243 -> 149, 187 -> 122, 148 -> 109, 195 -> 167; found when the cut-off study 6 file was regenerated, 2026-10-30), so whether the error is mostly a stable gain and offset differs between the two recording days or webcams.
- **Lag**: the gaze trails the dot by 40 to 300 ms (best-fit lag); the validation drift correction helps clearly in some sessions (study 6: 255 -> 136 px) and not in others.
- **Glasses** (reflections; a constant vertical offset between validation blocks was seen in study 6) did not make a consistent difference in study 7; the six sessions are different people, so glasses and person are confounded.
- **B is not better than A** (A better in 5 of 6 sessions; the difference is not significant with 6 pairs). B's CPU time is about twice the recording length.
- **Consequence for the product:** AOIs of about 65 px are hit only 4 to 5 % of the time when the participant was looking at them; with tolerance k = 2, about 60 %. The study design has to use large AOIs (`docs/RUNNING_A_STUDY.md`). A better gaze would shrink that limit; that is the point of tuning.
- Playback does not suffer on slow machines (simulated); the validation error also under-states the dot-video error a little (validation 141 px against 160 to 290 px on the dot video).

## 9. Where accuracy could come from (hypotheses, none tried unless it says so)

Treat this list as questions for the experiments, not decisions.

1. **Calibration data** (13 dots x about 20 usable frames = 260 samples for 10 features, two outputs). The vertical axis is the weak one and the calibration grid has only 3 rows plus quadrant centres. More dots near the top and bottom, repeated dots, a longer dot duration, or using the validation dots too in a final refit (they are never used for fitting now) give more rows of data. This is a protocol change (browser) as well as a pipeline change.
2. **Drift over time** (the strongest clue: error grows during the video and is not a constant offset). Candidates: head pose or distance changing after the calibration (the regression sees yaw, pitch, roll and `face_ratio` but **not** the head translation `tx, ty, tz`), posture and fatigue, the participant leaning back. Ideas: add translation to the features; per-frame head-pose compensation of the iris position; more validation blocks during long videos; a drift model with more than a linear interpolation (the current drift correction uses only the blocks before, between and after videos).
3. **The model class.** A ridge on 10 correlated features with CV choosing degree 1 or 2 may be underfitting the centre pull (slopes below 1) or overfitting the dots. Things to try: separate regularisation for x and y, feature interactions chosen by physics (iris position x head yaw), kernel ridge / small gradient boosting trained **across participants** on all calibration data (a pooled prior, then the per-session fit as a correction), monotone calibration curves per axis. Earlier finding (DECISIONS 2026-10-18): fewer features were worse than the 10; the plain linear model beat the quadratic one on most sessions.
4. **Better eye features.** Iris centre is noisy at 1280x720 and VP9 2.5 Mbit/s (frame-to-frame sd of the iris y about 0.004 to 0.005 in the feature's unit, DECISIONS 2026-10-18); averaging the two eyes, using the eye-corner geometry more carefully, using the 3D landmark z, using the other blendshapes (eyeLookIn/Out/Up/Down are produced by the same model, but only the blink scores are stored today, so this needs a change in `landmarks.py`), or a higher camera bitrate/resolution. Check the compression noise first: a recording bitrate or codec change is cheap to test.
5. **Pipeline B as a feature source for A**, not a replacement: B's angles as extra regressors next to A's 10 (not tried; B alone is worse). B's crop (landmark box x 1.2 square, not the face detector crop of the reference code) is a possible loss; so is the fixed batch.
6. **Temporal behaviour.** The 40 to 300 ms lag (a shift of the gaze series, the Kalman smoother's settings, or the sync offset) is cheap to test offline against the dot: shift the gaze series by the median best lag and see what it does to the error and the AOI hit rate. The best lag differs per session (40 to 300 ms), so look at what causes the spread.
7. **Quality gates.** Inclusion thresholds (0.7 valid, 0.12 validation error) are plan defaults, not tuned; with the dot data one can see which sessions a threshold would keep and what their error against the dot is.
8. **Evaluation noise.** There are only 10 to 12 dot sessions. A change that helps three sessions and hurts two is not an improvement. Use leave-one-session-out thinking (for any parameter chosen across sessions), report all sessions, and keep a held-out set.

What was already tried (so as not to repeat it): fewer features (worse); quadratic against linear (linear wins in most sessions, the CV decides); a constant gain/offset fitted on the first half of the dot video (does not help); drift-corrected against as-measured (helps in some sessions, default now); the fixed degree-2, alpha-1 model of the first browser gate (overfitted, replaced by the CV fit); Pipeline B (not better).

## 10. How to measure a change, safely

- **Data available** (never commit it, never edit it in place): the local stack's database and `data/studies/6` (H2, four dot sessions), `data/studies/7` (H3, six dot sessions, A and B), `data/studies/1..5` (earlier tests), exports `data/exports/study-h2.zip` and `data/exports/study-7.zip` (restore with `cli.py import-study`). The calibration and validation dots of **every** session are ground truth too: validation error (`quality.json`) is the faster metric, the dot video the more honest one. Start Docker Desktop, then `docker compose up -d`.
- **The lab** (`backend/lab/README.md`, DECISIONS 2026-10-30): `make lab ARGS="import-study 7"` copies sessions out of `data/`, `make lab ARGS="run <experiment>"` re-runs the product's stages with an experiment's `PipelineConfig` (about 5 s for the 10 dot sessions), `make lab ARGS="compare <experiment>"` judges it against `baseline` with the agreed rule. The lab's baseline reproduces the outputs in `data/` exactly (`docs/evidence/m6-tuning-baseline/baseline_lab.txt`). The old way: `docker compose exec -T api python - 7 < docs/evidence/m6-tuning-baseline/dot_eval.py`.
- **Experiments without touching `data/`**: copy a session (or a whole study) to a scratch data root (`cli.py export-session` / `import-session` into a scratch directory, or `cp -r`) and call the stage functions with a modified `PipelineConfig` on the copy: `run_calibration(root, study_id, session_id, config)`, `run_gaze(...)`, `build_quality(...)` (see `tests/test_quality.py` and `tests/conftest.py` for the pattern; `a_session` is a real recorded fixture session, `b_session` the same after B). Everything the stages need is under `root`; nothing else is global.
- **Changing a stage**: keep the TypeScript twin in step when the browser uses it (features, ridge, quick calibration); change thresholds only as named `PipelineConfig` fields (the plan's rule: no literals); every change that is a deviation from PLAN goes in `docs/DECISIONS.md` first with the evidence (numbers and session ids), and the human approves.
- **Tests**: `make lint`, `make test` (real PostgreSQL in a container, real MediaPipe, real ffmpeg, the recorded fixture session; 900+ backend tests, about 7 minutes), `make e2e` (the whole stack in a browser; about 3 minutes); stage tests must stay on real fixture data. The sessions in `data/` are not fixtures and not in git.
- **What needs the human**: anything that needs a real webcam or real people (new calibration protocol, dots, lighting, glasses) and the approval of deviations. When a task needs this, finish everything else, then write exactly what the human should do and report back.

## 11. Code map

| Area | Where |
|---|---|
| Browser: camera, recorder | `frontend/src/media/`, `capture.ts` |
| Browser: face model, features, quick calibration, gate, rate | `frontend/src/face/` |
| Browser: calibration dots, validation, screens, session flow | `frontend/src/calibration/`, `screens/`, `flow.ts`, `sessionPlan.ts` |
| Upload and event log | `frontend/src/upload/`, `eventLog.ts` |
| Shared definitions (landmark subset, protocol, model hashes) | `shared/` |
| Server stages A | `backend/worker/pipeline/` (`runner.py` lists the stages) |
| Server stages B | `features_b.py`, `model_b.py`, `runner_b.py`, `quality_b.py` there; `app/pipeline_b.py` (names, stale mark) |
| All thresholds | `backend/worker/pipeline/config.py` (`PipelineConfig`), analysis: `worker/analysis/config.py` |
| Jobs, handlers | `backend/worker/handlers.py`, `run.py`; `app/jobs.py` |
| Admin pages, session inspector, plots | `backend/app/api/admin_*.py`, `app/admin_ui/templates/` |
| Moving-dot scoring | `worker/pipeline/moving_dot.py`, `cli.py moving-dot-report`, `docs/evidence/m6-tuning-baseline/dot_eval.py` |
| Experiment lab (T13) | `backend/lab/` (README there), data in `lab_data/` (gitignored), tests `backend/tests/test_lab.py` |
| Tests and fixtures | `backend/tests/` (`conftest.py`: `a_session`, `b_session`), `shared/fixtures/`, `e2e/` |
