# Decisions and deviations from PLAN.md

Every agreed deviation from `docs/PLAN.md`, and every choice the plan leaves open (library versions, model weights and their sha256, tuned thresholds). Newest first.

Format:

```
## YYYY-MM-DD — <short title>
- Plan section: §x.y
- Decision: what we do instead / what was chosen
- Reason:
- Approved by: <name> (or "plan leaves open")
```

---

## 2026-10-18 — Milestone 3 pilot result (H1) and deferred tuning
- Plan section: §14 (Milestone 3 "done when"), §8.6, §8.8, §12.4
- Decision: the acceptance run (two real-webcam sessions on the moving-dot video, see PROGRESS "H1 result") meets the criterion of DECISIONS 2026-10-16 item 9 (median error at most 0.15 of the viewport diagonal, at least 80 % valid): 0.098 and 0.046, 98 % valid. Observations, none acted on yet (human, 2026-10-18: "continue with the development plan, fine-tune the models for better precision later"):
  1. Vertical accuracy is worse than horizontal: the iris moves 0.04 to 0.05 eye widths vertically across the screen against 0.19 to 0.24 horizontally.
  2. Session A (glasses, three calibration attempts, 153 s before the video) is noisier than B (no glasses): frame-to-frame jitter in the validation points 93 against 31 px. The per-frame landmark noise is the same (iris y frame-to-frame sd 0.0047 against 0.0041), so glasses are one possible cause among posture and fatigue; two sessions cannot tell.
  3. Both sessions show the same +160 ms gaze lag behind the dot (human latency, or a fixed latency in the webcam or display chain; not separated).
  4. The drift correction of §8.8 (mean offset of 5 validation points, interpolated) does not help on the moving dot: B 92 to 115 px, A 197 to 190 px, although the validation error itself improves (A 0.074 to 0.042, B 0.056 to 0.050). The offset mixes drift with the compression at those five positions. Both columns are kept; use the uncorrected ones until a better rule exists.
  5. Offline checks on the same two sessions: the 10-feature ridge beats every smaller set tried (iris average with yaw and pitch, with roll, with openness, iris only, left/right iris with yaw and pitch) on validation error and on the moving dot in both sessions; linear beats degree 2 by a wide margin in the cross-validation (A 177 against 235 px, B 116 against 201 px).
  6. The browser gate and the server's validation agree (B: 0.069 and 0.040 against 0.072 and 0.040).
- Tuning candidates for later (they need more pilot sessions, with and without glasses, ideally from other participants): the drift rule, the vertical axis (more calibration points near the top and bottom, repeated dots), the gaze lag, the smoother and quality thresholds (DECISIONS 2026-10-16).
- Reason: record the acceptance evidence and the decision to continue with the plan.
- Approved by: Nágó Dániel

## 2026-10-18 — Milestone 3 T15: debug replay video and webcam playback
- Plan section: §12.2, §10; DECISIONS 2026-10-14 item 3, milestone 3 item 17
- Decision:
  1. **Job kind `render_debug`** (migration `0002`: the `job_kind` CHECK now lists it; the downgrade deletes replay jobs and restores the old list). The worker's handler draws `derived/debug/replay.mp4` and leaves the session status alone. One open job of either kind per session at a time: a render and a re-run exclude each other (both rewrite files the other reads). Rendering needs a processed session with gaze; the button says why when it is off.
  2. **What the film shows:** see DATA_FORMATS. Drawn at 30 fps along the client clock for the stretches with a video on screen; raw, smoothed and drift-corrected gaze (the plan names raw and smoothed; the corrected one is added because the pilot is about the validation error), fixation rings, webcam with landmarks, timeline bar. Pipeline B is left out until M5. The webcam inset is optional (not drawn when the recording is gone), the stimulus video is not (the replay says to upload it again when missing: imported sessions need it).
  3. **Participant video is marked and gated:** the file's banner says it contains participant video; the replay and the webcam recording are served only to a logged-in admin (401 otherwise), never cached, with range requests so they can be searched, and every access is logged (`participant video served to an admin`, with the session id). The webcam recording has its own page that shows a warning and plays `raw/webcam.webm`; it is not embedded in the session page. A session whose raw video was deleted (`raw_video_deleted_at`) answers 404.
  4. `cli.py render-debug <id>` renders here and now and prints the warning.
  5. Looked at frames of the recorded session by eye and made the text readable (a darkened panel behind it).
- Reason: details the plan leaves open for the replay.
- Approved by: plan; details plan leaves open (flag if you object)

## 2026-10-18 — Milestone 3 T14: admin session inspector
- Plan section: §12.2, §6 (admin), §10; DECISIONS 2026-10-14 item 2
- Decision:
  1. **The session page** (`/admin/sessions/{id}`) gets: the quality report (headline numbers and the validation blocks, the whole `quality.json` in a collapsed block), the stage table (what each stage does, last duration, summary, "Re-run from here"), the last five processing jobs (a failed job shows its log), and the debug plots as images. The sessions list gets an "Included" column and the participant results page an inclusion line with a link to the report (as DECISIONS 2026-10-14 said later milestones would).
  2. **Re-run** queues a `process_session` job with `from_stage` (the worker does the work; nothing runs in the api process): `POST /admin/sessions/{id}/rerun` (form, CSRF token) and `POST /admin/api/sessions/{id}/rerun` (JSON `{from_stage}`, CSRF header). Allowed for uploaded, processed or failed sessions with no queued or running job for them (409 otherwise); an unknown stage is 422. "Re-run from here" on `ingest` is "process again from the start".
  3. **Plots are served only to a logged-in admin** by `GET /admin/sessions/{id}/debug/{name}`, where `name` must be one of the five known plot names (no directory listing, no other file can be reached), `Cache-Control: no-store`. They are not under the public `/media` route.
  4. **Stage and plot names live in `app/pipeline_names.py`** (a light module) so the api process does not import the pipeline (MediaPipe, matplotlib); the runner and the plots take them from there.
  5. The webcam recording and the debug replay video are not on the page yet (T15).
- Reason: details the plan leaves open for the inspector.
- Approved by: plan; details plan leaves open (flag if you object)

## 2026-10-18 — Milestone 3 T13: debug plots
- Plan section: §12.2
- Decision:
  1. **Plots are the last stage (`plots`)** after `quality`, so they can use the quality report; the stage list is now ingest, landmarks, features, sync, calibration, gaze, quality, plots. The runner writes the stage results into `quality.json` after a run that included `quality` or `plots`.
  2. **A plot that cannot be drawn does not fail the session.** Missing inputs mean skipped (with the reason, and the old picture is removed); an exception is logged and recorded in the stage summary (`errors`) but the other plots are still drawn and the session stays `processed`. The data is fine without the picture, and the plan's "flag it and move on" is met by the record in `quality.json`; no session flag is added.
  3. **`calibration.png` fits every attempt again** (`fit_attempt`, extracted from `fit_calibration`) so that failed attempts can be drawn, and draws the validation points that followed each attempt with that attempt's model. Pipeline B is not drawn (M5).
  4. **Added:** the moving-dot plot (path against gaze) through `cli.py moving-dot-report --plot`, as the pilot check's picture (DECISIONS milestone 3 item 9); not a stage.
  5. Looked at the plots of the recorded session by eye and changed what was wrong: the sync panel for one window showed numeric noise (now a note), calibration predictions far outside the screen were cut off without notice (now counted), and wild raw predictions squashed the gaze plot (now the axes show half a screen around the screen).
- Reason: details the plan leaves open for the debug plots.
- Approved by: plan; details plan leaves open (flag if you object)

## 2026-10-18 — Milestone 3 T12: CLI
- Plan section: §11.4, §12.3, §12.4
- Decision:
  1. **`cli.py process <id> [--from-stage S | --only S] [--keep-intermediates]`** runs the pipeline in the CLI process through the same handler as the worker (status, flags and the report in the database are handled alike) and prints the stage timings. It does not go through the queue; the admin button (T14) will. The "intermediate" file is the concatenated webcam before the remux, kept in `derived/intermediates/`.
  2. **`export-session` / `import-session`** (layout in DATA_FORMATS): the plan's `raw/`, `derived/` and `meta.json` plus `chunks.json` (the chunk table rows ingest needs). Added `--no-raw-video` (like `backup.sh`) and a printed warning when the video is included. Stimulus videos are not in the archive: re-upload them on the development instance if the debug replay (T15) is wanted. Import creates a missing study from the config snapshot and moves the id sequence past it.
  3. **`moving-dot-report <id> [--path F] [--video-id V] [--json]`** is the Milestone 3 pilot check with DECISIONS item 9's criterion (median error at most 0.15 of the viewport diagonal and at least 80 % valid playback frames; both are `PipelineConfig` values `dot_max_median_error_norm`, `dot_min_valid_fraction`). Exit status 0 pass, 2 fail. It reports uncorrected and drift-corrected errors (median, mean, 90th percentile), the errors during the dot's pauses and movements apart, and the gaze lag (the shift of the dot's path that fits best; positive means the gaze is behind). Its ground truth `moving_dot_path.json` is copied into the backend image as `/shared/moving_dot_path.json`.
  4. `cli.py` gets `settings()` (renamed from `_settings`) and `session_factory()` so tests can point it at a test database.
- Reason: details the plan leaves open for the CLI.
- Approved by: plan; details plan leaves open (flag if you object)

## 2026-10-18 — Milestone 3 T11: stage runner, job handler, enqueue at /complete
- Plan section: §6, §8, §13
- Decision:
  1. **`/complete` queues `process_session`** (reverses DECISIONS 1b scope item 2, as planned): once per session, on the first completion only, and not for a session without chunks (`empty`). A session that never recorded the webcam (the participant reloaded before the camera started) is queued, fails in the ingest stage with "webcam: there are no chunks" and ends `failed`; that is loud, and the e2e test expects exactly this.
  2. **Stages and order:** ingest, landmarks, features, sync, calibration, gaze, quality (`worker/pipeline/runner.py`). A run can start at a stage (`from_stage`) or run one (`only`); the stages before keep the summary from the last run that included them (read back from `quality.json`). Each stage's duration and summary are in `quality.json` under `stages`.
  3. **Failure:** a stage that raises stops the run (`StageFailed`, naming the stage); the earlier stages' files stay. A failed calibration is not a failure: the gaze stage is skipped and its old files are removed (they are derived, so stale ones would mislead), the quality stage excludes the session.
  4. **Session state:** the handler sets `processing` before the stages (so a dead worker is recognised), then `processed` when the quality report exists, `failed` on an exception. `only` of an early stage on a never-processed session leaves it `uploaded` (no report yet). The pipeline owns the flags `sync_*` and `calibration_failed`: they are recomputed from `sync.json` and `calibration_a.json` at the end of every run, so a later good run removes them.
  5. **The handler gets its own database sessions** and holds none while the stages run (they take minutes); the chunk table rows for ingest are read before.
  6. `sessions.quality` stores the whole `quality.json` (PostgreSQL JSONB does not keep the order of keys; the file does).
  7. e2e (`scripts/e2e.sh`, `scripts/e2e_check_derived.py`): after the Playwright tests the script waits for the queue to empty, then checks every derived file of each session that recorded: exists, columns as in DATA_FORMATS, one row per webcam frame, the stages in `quality.json`; exactly one job may fail, for the missing webcam.
- Reason: wiring the stages to the job queue and the participant API as the plan says (§6, §8).
- Approved by: plan; details plan leaves open (flag if you object)

## 2026-10-18 — Milestone 3 T10: quality report and inclusion
- Plan section: §8.10, §4.1, §12.1
- Decision:
  1. **Fixations from valid or interpolated samples (T9 item 4) confirmed** by Nágó Dániel ("you can keep it how you've done it").
  2. **Inclusion rule** (the plan names the thresholds but not the quantities): the validation error is the mean `error_norm` over the validation blocks with data, uncorrected (the corrected value is reported too); the valid fraction is over the frames with a video on screen (a pause counts as playback). A session with no validation data is left out (`no_validation_data`: the accuracy cannot be checked), and so is a debug session (PLAN §12.1: excluded from analysis by default). Sync flags are reported only.
  3. **Face-lost episodes** come from the server's own detections (a stretch of frames without a face of at least 500 ms, the browser's debounce), not from the browser's events, which are counted separately; both are in the report.
  4. **Face size flags** (`min_face_ratio_warn`, `max_face_ratio_warn`, reserved for M3 by DECISIONS 2026-10-16): the share of playback frames outside the band, where the band is those multipliers times the median face ratio during the calibration dots (the device check frames are not recorded).
  5. **Stalls:** a `video_stall` with media time 0 belongs to the video's start (the initial `waiting`) and is counted separately.
  6. New named values in `PipelineConfig`: `quality_face_lost_min_ms` (500) and `quality_frame_gap_ms` (100).
- Reason: details the plan leaves open.
- Approved by: item 1 Nágó Dániel; the rest plan leaves open (flag if you object)

## 2026-10-18 — Milestone 3 T9: fixations and the gaze table
- Plan section: §7, §8.2, §8.9
- Decision (readings of what the plan leaves open; flag any you object to):
  1. **`x_vid`, `y_vid` come from the smoothed gaze** (`x_sm`, `y_sm`), the column the analysis will use; the raw `x_px` has no video coordinates. The content rectangle is the one of the last `viewport_change` event (null when no video is shown).
  2. **Extra columns** in `gaze_a.parquet`: `interpolated`, and the drift-corrected `x_px_dc`, `y_px_dc`, `x_sm_dc`, `y_sm_dc`, `x_vid_dc`, `y_vid_dc` (the plan asks for "drift_corrected gaze columns" without naming them). Null when no validation block gave data; with a single block the correction is that block's constant offset (DECISIONS milestone 3 item 7), although the plan says "if several blocks exist".
  3. **Phase:** `calibration` / `validation` for the span from the first to the last event of a run of those dots, `playback` while a video is on screen, else `other`. Frames in a pause or stall are `playback` with a null `stim_time`.
  4. **Fixations use the smoothed samples that are valid or interpolated**, not only the valid ones the plan names: interpolation exists to bridge short gaps, and a blink inside a fixation must not split it. A run ends at a frame without a smoothed value, a change of video or a change of viewport size. The dispersion limit uses the viewport width at the start of the run.
  5. **Duration** of a fixation is the time between its first and last sample (so a 100 ms minimum needs four 30 fps frames). The dispersion is the sum of the x and y ranges (I-DT as published).
  6. Fixation centroids are given in px and video coordinates, both uncorrected and drift-corrected; the analysis (Milestone 4) chooses.
  7. New named values in `PipelineConfig`: `fix_dispersion_width_fraction` 0.05, `fix_precision_factor` 2, `fix_min_duration_ms` 100 (all from the plan).
- Reason: details the plan leaves open.
- Approved by: plan leaves open (flag if you object)

## 2026-10-18 — Milestone 3 T8: validation and drift
- Plan section: §8.8
- Decision (readings of what the plan leaves open; flag any you object to):
  1. **Blocks scored:** the validation blocks from the calibration attempt that was used onwards. An earlier failed attempt's block is not scored (its calibration is not the model that is applied). Blocks are labelled from the video events, not from the gate results (a study without the gate has none).
  2. **Frames and predictions:** valid frames only, raw (unsmoothed) predictions, the same window as the training window (700 ms before to 100 ms after the click, `calib_window_*`).
  3. **Precision** is the RMS of the distance between the predictions of frames that directly follow each other; pairs with a dropped or invalid frame in between are not used, so a point with no such pair has no precision. The block's precision is the mean over its points; the session's (for I-DT, DECISIONS milestone 3 item 8) the median over the blocks.
  4. **Offset vector** of a block: the mean over its points of (median prediction minus target). Its time is the mean click time of the points with data. The correction subtracts the offset, so it removes the calibration's own constant bias too (the first block is included, as the plan says "each validation block").
  5. **Corrected error** of a point: the error of its median prediction after subtracting the drift at its own click time (interpolated), so with several blocks it is not exactly zero, which is what shows whether a linear drift explains the error.
  6. On the recorded session (fake camera, not looking at the dots) the pre-video block has predictions on screen but the block after the video has none: the model extrapolates far off screen when the head turns, and the validity rule (§8.7) leaves those frames out. The block then has `points_with_data` 0 and no drift entry.
- Reason: details the plan leaves open.
- Approved by: plan leaves open (flag if you object)

## 2026-10-18 — Milestone 3 T7: prediction, validity and smoothing
- Plan section: §8.7
- Decision (readings of what the plan leaves open; flag any you object to):
  1. **Process noise.** The plan asks that "a 300 px step settles within about 60 ms on synthetic data". "Settled" is read as DECISIONS milestone 3 item 6: within 10 % (30 px) of the new level from 60 ms after the step on (and, because the smoother sees the future, within 30 px of the old level until 60 ms before it). With 15 px of measurement noise the white-noise acceleration density `smooth_process_noise` = 3e7 px^2/s^3 gives 17 ms at 30 fps, 25 ms at 60 fps and 15 ms at 100 fps; it is also close to the value that reduces fixation noise most (RMS 15 px to about 10 px). Smaller values (1e6) are slower and noisier, larger ones (1e9) follow the noise. The measurement noise of 15 px is the synthetic test's, not a measured value of real webcam gaze (likely larger); retune `smooth_meas_std_px` and `smooth_process_noise` on pilot data (deferred, DECISIONS 2026-10-16).
  2. **Gap length.** An invalid gap is interpolated when the valid samples on both sides are at most 150 ms apart (so three lost frames at 30 fps, 133 ms, are filled and four, 167 ms, are not); the same 150 ms separate segments.
  3. **Interpolated samples count as measurements** in the smoother (the plan says "interpolate, then smooth"), with the same noise as real ones.
  4. **Validity uses the viewport in force at the frame's time**, not the one of the calibration. The predictions stay in the coordinates the model was trained in; a viewport change after calibration (leaving fullscreen) is handled by the playback pause and by the `x_vid` mapping (T9), not here.
  5. Speed: 10 minutes of frames (18 000) take 0.13 s to smooth in plain Python.
- Reason: details the plan leaves open.
- Approved by: plan leaves open (flag if you object)

## 2026-10-18 — Milestone 3 T6: calibration fit
- Plan section: §5.5, §5.6, §8.6
- Decision (readings of what the plan leaves open; flag any you object to):
  1. **Outlier rule.** "Beyond 2.5 MAD of the dot's median prediction" is: the distance of each sample's prediction to the dot's median prediction (per coordinate), compared with 2.5 times the median of those distances (an unscaled MAD of the 2-D scatter; for Gaussian scatter this drops about 1.5 % of the samples). A dot whose MAD is 0 drops nothing. After the rejection the model (degree, alpha, weights) is chosen again on the remaining samples, so the recorded cross-validation error belongs to the final data.
  2. **CV error** is the browser's definition: mean over the left-out dots of the mean per-frame distance to the dot, in px. The alpha grid has one value per decade. Ties go to stronger regularisation, and between the two models to the plain linear one.
  3. **Same feature vector as the browser** (the 10 columns of `featureVector`; head translation is not a regressor), as DECISIONS 2026-10-16 kept it.
  4. **Failure is a result, not an exception** (as in item 15 of the scope entry): no calibration dots, more than 3 sparse dots, or no possible fit give `status: "failed"` and `calibration_a.json` without a model. Dots that were never clicked have no training window and count as sparse.
  5. **The training window is not clipped to the dot's appearance.** The plan's window (700 ms before to 100 ms after the click) is used as written; with the plan's 1500 ms dots the click is at least 900 ms after the dot appears, so the window starts after the onset. In the e2e test (700 ms dots, instant clicks) the window starts before the dot appears; that data is for plumbing only.
  6. `ridge.py` is a numpy version of the browser's ridge, tested against scikit-learn and against the browser's fixture `shared/fixtures/ridge_fit.json` (1e-6).
  7. New named values in `PipelineConfig`: the two window lengths and the 5-frames/3-dots rule (plan), the alpha grid (plan), `calib_outlier_mad` 2.5 (plan).
- Reason: details the plan leaves open for the calibration fit.
- Approved by: plan leaves open (flag if you object)

## 2026-10-18 — Milestone 3 T5: sync and the stimulus-time map
- Plan section: §8.2
- Decision (readings of what the plan leaves open; flag any you object to):
  1. **Signal.** "Horizontal iris position minus eye-corner midpoint, plus head yaw, z-scored" is read as two channels (the eye-local iris x, which already is that difference, averaged over both eyes; and yaw), each z-scored, correlated one by one, the two correlations averaged. Summing the two into one signal could cancel when they move in opposite directions; averaging the correlations cannot. Blinks and frames without a face are left out of both signals.
  2. **Windows.** Consecutive windows of 60 s over the webcam pts, a rest under 30 s joins the last window (the 30 s is a new named value, `sync_min_window_s`). Each window's lag is searched within +-1000 ms of the whole-session lag. A window with a peak under 0.3 or at the edge of the search is left out of the fit.
  3. **Fit.** scikit-learn's `HuberRegressor` (epsilon 1.35) on lag against pts; `b = 1 + slope/1000`. With fewer than two usable windows in a long session the slope is taken as 1 and the session gets `sync_drift_unknown`.
  4. **Flags instead of failures.** A weak or edge result flags the session (`sync_weak_peak`, `sync_lag_at_limit`, ...); only a missing `recorder_start` event is an error. Without usable browser features or server frames the coarse map is used with `sync_coarse_only`.
  5. **On the recorded session** the server and browser signals correlate at 0.998 with a lag of -4 ms: both models see the same frames, and the `recorder_start` event is logged at the moment the first recorded frame (pts 0) is made, so the coarse map is already right.
  6. **Stimulus gaps** are found from the frames themselves (wall time advanced more than media time by over `stim_gap_tolerance_ms`, 50 ms, or media time went back), not from `video_pause`/`video_stall` events, so dropped frames and display jitter (the recorded session shows 33 or 50 ms steps for a 25 fps video) are not gaps. A video is on screen from its first presented frame to its `video_ended` event.
  7. `sync_max_gap_ms` (150 ms), `sync_min_overlap_s` (1 s), `sync_min_window_s` (30 s) and `stim_gap_tolerance_ms` (50 ms) are new named values in `PipelineConfig` that the plan does not give.
- Reason: details the plan leaves open for the sync stage.
- Approved by: plan leaves open (flag if you object)

## 2026-10-18 — Milestone 3 T4: features table and the pipeline config
- Plan section: §8.4, §1
- Decision:
  1. **`PipelineConfig`** (`backend/worker/pipeline/config.py`, DECISIONS milestone 3 item 10) now exists with the two blink thresholds (0.5 and the starting value 0.15 from milestone 2); later tasks add their fields. A stage takes it as an argument and records what it used (here as `config` in the Parquet metadata).
  2. **The browser keeps its own copy of the blink thresholds** (`frontend/src/face/quickCalib.ts`): the browser gate is only a pass/fail check and the server's fit is authoritative (PLAN §5.6), so tuning the server value does not have to change what participants see. If the two should move together, say so and a shared definition goes into `shared/protocol.json`.
  3. A frame without a face has all features null and `blink` false (it is invalid because it has no face).
- Reason: details the plan leaves open.
- Approved by: plan leaves open (flag if you object)

## 2026-10-18 — Milestone 3 T3: server-side landmarks
- Plan section: §8.3
- Decision:
  1. **Decoder and colour.** Frames are decoded with PyAV and converted with its default `rgb24` conversion; no colour matrix is forced. On the face clip this differs from OpenCV's conversion by about 1/255 per pixel (at most 18) and the landmarks by about 0.01 normalised units, the same size as the browser-vs-Python difference seen in milestone 2. Not worth tuning; the sync (T5) and the calibration absorb it.
  2. **Faces.** Up to 2 faces are looked for and the first is kept, as in the browser (`MAX_FACES`). A face without a transformation matrix or without the `eyeBlink` blendshapes is an error, not a silent "no face" (as in the browser, DECISIONS 2026-10-16).
  3. **Model file.** `FACE_LANDMARKER_MODEL` (already set in the backend image) is the new setting `face_landmarker_model`. The tests use that file when it exists, otherwise they download the model once into pytest's cache with `shared/download_model.py` (sha256 checked), so CI needs no extra step but does need network.
  4. **mypy.** `mediapipe` and `pyarrow` ship no types: `ignore_missing_imports` for those two packages only.
- Reason: details the plan leaves open for the landmark stage.
- Approved by: plan leaves open (flag if you object)

## 2026-10-16 — Milestone 3 T2: ingest and the session fixture
- Plan section: §7, §8.1, §13
- Decision:
  1. **The chunks are checked against the `chunks` table, not against the files.** `ingest()` takes the table's rows (stream, seq, size, sha256) as an argument, so the stage stays a function of its inputs and a chunk that changed on disk is found. The runner (T11) reads the rows from the database; `export-session` / `import-session` (T12) must carry them (as `chunks.json`, like the fixture).
  2. **Per-frame pts are written to `derived/webcam_frames.parquet`** (`index`, `pts`), a file §7 does not list. The pts come from the packets of the remuxed file (cheap, no decoding); the landmark stage (T3) records the pts of the frames it actually decodes.
  3. **Streams:** `webcam` and `events` must have chunks; `features` and `stimulus_frames` may be absent (a debug session can skip the video). A chunk that is not valid gzip, a line that is not a valid record, or an existing assembled file that differs makes ingest fail loudly (the session's job fails with the reason). The server already flags such sessions (`events_invalid` etc.) at `/complete`; ingest does not try to continue with part of a stream.
  4. **Session fixture:** `shared/fixtures/session_e2e/` (3.0 MB: 23 chunks, the assembled JSONL files, `meta.json`, `chunks.json`) from one session of the e2e run (Chromium's fake camera playing `e2e/fixtures/face.mp4`; webcam VP9 854x480, 11.3 s, 314 feature records). `E2E_EXPORT_SESSION=<dir> make e2e` exports it and `scripts/make_session_fixture.py <dir>` writes the fixture. The gaze does not follow the dots, so it tests plumbing and the maths of each stage, not accuracy. It is 11 s long: sync's 60 s windows (T5) get the `sync_short` path on it.
  5. Dependencies added: numpy, scipy, pandas, pyarrow, matplotlib, scikit-learn (approved), pandas-stubs (dev).
- Reason: details the plan leaves open for the ingest stage.
- Approved by: plan leaves open (item 4's size limit and item 5 approved by Nágó Dániel; flag the rest if you object)

## 2026-10-16 — Milestone 3 scope and open choices
- Plan section: §4, §5.6, §7, §8.1 to §8.10, §12.2, §12.3, §14
- Decision:
  1. **Session fixture.** A real MediaRecorder session (webcam chunks, features, events, stimulus frames) is captured from the e2e run (fake camera playing the human's face clip) by `scripts/make_session_fixture.py`. It is committed if it is 10 MB or less; if it is larger, the agent asks before committing.
  2. **`landmarks.parquet` (§8.3)** holds per frame: `pts`, `face`, 478 x 3 float32 landmarks, `matrix[16]` (row-major), `blink_l`, `blink_r`. Not all 52 blendshapes.
  3. **Attempts (§5.6, §8.6).** The server derives which calibration dots belong to which attempt from event order (calibration block, validation block, `calib_gate_result`); the dot events get no `attempt` field.
  4. **`raw/webcam.webm` is written by ingest** although raw data is otherwise immutable (§1): it is a deterministic assembly of the immutable chunks, like `events.jsonl`. The chunks are never changed.
  5. **`conf` (§7)** is 0 when invalid, 0.5 when interpolated, otherwise 1 minus a linear penalty for how far the raw prediction lies outside the viewport (0 at the edge, 1 at the 15 % limit).
  6. **Smoother step response (§8.7):** "settled" means within 10 % (30 px) of a 300 px step, on synthetic data with measurement noise of 15 px; it settles within about 60 ms.
  7. **Drift correction (§8.8):** linear interpolation of the offset vector between the mid-times of the validation blocks, constant before the first and after the last; with one block, constant. Applied to every phase. Degrees of visual angle are always null in M3 (stations were dropped in 1b).
  8. **I-DT (§8.9):** dispersion = x-range plus y-range; "session validation precision" = median over the validation blocks.
  9. **Moving-dot acceptance (§14):** median `error_norm` against the ground-truth path at most 0.15, at least 80 % valid playback samples, and the human looks at the debug replay.
  10. **Pipeline thresholds** live in a `PipelineConfig` with the plan's defaults in code (not editable per study); the values used are written into each derived file.
  11. **Sync (§8.2):** a named `sync_min_peak_correlation` (0.3) only flags the session, it never blocks. A session shorter than two 60 s windows uses one window and gets the flag `sync_short`.
  12. **Quality flags and inclusion (§8.10, §4.1).** `min/max_face_ratio_warn` (reserved in M2) are multipliers of the median face ratio during the calibration phase. Inclusion uses the mean `error_norm` over all validation blocks (uncorrected; corrected values are reported too) and the valid fraction over playback only.
  13. **scikit-learn is added** (it is in §3's list). Where it saves code (Huber fit, reference for ridge) it is used.
  14. **Stale `running` jobs (R2):** at worker start, `running` jobs with `attempts` below `JOB_MAX_ATTEMPTS` (default 3) are requeued, the others are marked `failed` ("worker died"); the session status goes back to `uploaded` when requeued. Safe because `WORKER_CONCURRENCY` is fixed at 1 and the stages are idempotent.
  15. **Calibration `failed` (§8.6)** is a data problem, not a crash: the later stages are skipped, no `gaze_a.parquet` is written, the session is `processed` with the flag `calibration_failed` and is excluded. Status `failed` is for crashes.
  16. **Scope of §12.3 `[M1-M3]` items:** `export-session` / `import-session` are in M3 (T12). The Jupyter notebook and the admin diagnostics page move to Milestone 6.
  17. **Debug replay job (§12.2):** a new job kind `render_debug` (migration; a small addition to the kinds in §4). The replay shows Pipeline A only in M3; Pipeline B joins in M5.
  18. **Carry-overs stay in Milestone 6:** the total inflated cap per stream in `/complete` (M2 review major 2) and the vitest 4 upgrade (`npm audit`).
- Reason: answers to the numbered questions of `/milestone-start 3`.
- Approved by: Nágó Dániel ("you can get scikit. Others seem good, you can start", 2026-10-16; all recommendations accepted)

## 2026-10-16 — Milestone 2 review decisions and admin additions
- Plan section: §4, §4.1, §5.4, §5.6, §6, §12.1
- Decision:
  1. **Admin study pages (addition, after the first real run).** The study forms pick the playlist from a table of videos (tick, type the order; unnumbered ticked videos come last, by id) instead of ids in the JSON; the text box holds every other setting, and the table always wins over a `playlist` key typed into the box. `POST /admin/studies/{id}/debug` switches `debug_allowed` on or off and the study page then shows the link with `?debug=1`. The JSON API is unchanged. Approved by Nágó Dániel ("create a button for debug toggle ... add a video selection").
  2. **A feature record with a face may have `brightness: null`** (the pixels could not be read; the device check then skips the brightness check). The schema, the shared example `feature_record_unmeasured_brightness` and DATA_FORMATS say so; before, the server would have refused the whole features stream for one such frame.
  3. **The face model must not give half a result:** a face without landmarks or matrix, or without a blink blendshape, is an error (fatal in the session), not a silent "no face" or "no blink". A camera that reports no frame rate is assumed to deliver the study's `recording.fps`, and the browser logs a `client_error` event saying so; a failing stop of the recording during cleanup is logged the same way.
  4. **`quality.min_face_ratio_warn` and `max_face_ratio_warn` are reserved for the M3 quality flags** (the reading as multipliers of the device-check face ratio stays the working assumption). The browser shows the ratio relative to the device check in the debug panel only; no warning is shown to participants.
  5. **Gate disabled** (`calibration_gate.enabled: false`): no `calib_gate_result` is logged at all, also not for later validation blocks, and the session is never flagged `calibration_gate_failed`.
  6. Left as they are by the human: the device check preview is the mirrored camera image with a text line and a progress bar (no face box); the banner for a missing face also shows during calibration and validation; `recorder_dataavailable` is logged after the chunk was stored in the browser.
- Reason: `/milestone-review 2` (plan-reviewer) found these; the first and the last two points are choices the plan leaves open.
- Approved by: Nágó Dániel (2026-10-16: fix major 1 and 3, minor 1, 3, 4, 5, 7, 8; reserve the warn ratios for M3; leave the device check as it is)

## 2026-10-16 — Browser model chosen by cross-validation; smoothed gaze dot (deviation from §5.6 step 3)
- Plan section: §5.6 (step 3: degree 2, alpha fixed 1.0), §8.6, §12.1
- Decision: the browser gate fits the quick model as §8.6 describes the server's fit: leave-one-dot-out cross-validation over a plain linear ridge and the degree-2 ridge, each with alpha on the log grid 1e-3 to 1e3, and keeps the best (ties go to the simpler model). The chosen degree, alpha and CV error are shown in the debug panel; the gate thresholds and everything else in §5.6 are unchanged. The live gaze dot (debug only) is smoothed with a one-euro filter (0.5 Hz at rest, beta 0.0005) and kept inside the viewport.
- Reason: the human's first real run (study 3): the fixed degree-2 model on 10 correlated features and 301 frames overfitted; against the moving-dot ground truth 25 % of its predictions fell more than 15 % outside the screen and the 90th-percentile error was 1.17 screen diagonals. The cross-validated choice on the same run is a linear ridge with alpha 10 (validation error 0.079, 90th percentile 0.36); smoothing halved the frame-to-frame travel of the dot without raising its median error (0.123 to 0.114 to 0.121 depending on beta). The same data also showed that eyelid openness and face size help on the static calibration dots but hurt on moving targets (iris and head angles alone: median 0.125, 99 % on screen); one subject only, so the feature set stays as in §8.4 until more pilot data exists.
- Approved by: Nágó Dániel ("yes. Lets try your two ideas", 2026-10-16)

## 2026-10-16 — Dropped stimulus frames are counted from the media time (deviation from §5.4)
- Plan section: §5.4 ("check `presentedFrames` gaps")
- Decision: a dropped stimulus frame is a step of the media time of more than one frame duration between two `requestVideoFrameCallback` calls (`round(step x fps) - 1`; the video's `fps` comes with the video in the session response), not a gap in `presentedFrames`. `presentedFrames` is still logged.
- Reason: the human's first real run (Chrome, Mac, display scale 2) showed `presentedFrames` rising by 2 for each frame of a 30 fps video (1162 of 1168 steps), which made every frame look dropped, so the rate control cut the face model to its 8 fps floor during playback (9.6 and 8 fps measured against 29 fps before). The media time of the same frames was a clean 33 ms step.
- Approved by: Nágó Dániel asked for the cause of the jumpy gaze dot; this was found while answering (flag if you object)

## 2026-10-16 — Milestone 2 implementation choices (T4 to T13)
- Plan section: §5.2, §5.3, §5.4, §5.6, §8.4, §12.1, §12.4, §13
- Decision (all are the simplest reading of what the plan leaves open; flag any you object to):
  1. **Face model in a classic worker.** MediaPipe's WASM loader needs `importScripts`, which module workers lack (checked in Chromium: "ModuleFactory not set"). `scripts/build-face-worker.mjs` (esbuild, now a dev dependency) bundles `src/face/modelWorker.ts` to `/mediapipe/face-worker.js`, a classic worker, before `vite` and `vite build`; it runs in the same way in dev and production. CPU delegate; init about 130 ms, about 10 ms per frame on a laptop (headless).
  2. **Matrix layout.** The browser delivers the transformation matrix column-major; compared on the same clip with the Python API it matches after a transpose (landmarks agree within about 0.01 normalised units; the frames of the two decoders differ slightly). Feature records hold it row-major.
  3. **Feature record `t`** is the time the webcam frame was presented to the page (the `requestVideoFrameCallback` time), not when the model finished (corrects the wording of T1 item 2 docstrings). Features are recorded only while the recording runs; the device check frames are not.
  4. **Model rate** (DECISIONS item 10 of the scope entry): `AdaptiveRate` reduces to 75 % of the rate when more than 5 % of the stimulus frames of a 2 s window were dropped (at most one step per window), never below 8 fps, and returns by 25 % steps after 5 s without drops. `FrameGate` lets frames through on a fixed schedule so a 24 fps target on a 30 fps camera averages 24 fps. All are named constants in `face/adaptiveRate.ts`; starting values for real laptops. The stimulus video's fps is sent to the browser (`VideoRef.fps`, an addition to the §6 response).
  5. **Face lost:** `face_lost` / `face_regained` are logged when confirmed (500 ms), not at the onset; `lost_ms` is exact. The banner (3 s without a face) never pauses anything. The events are only logged while recording.
  6. **Blink rule:** `eyeBlink > 0.5` for either eye, or either eye's openness below 0.15 (starting value; the clip's blinks have 0.07 to 0.14, open eyes 0.3 to 0.39).
  7. **Gate details:** training window 700 ms before to 100 ms after the click, also used for the validation points; the cause is chosen in the order few frames, low brightness (mean face brightness below `quality.brightness_min`), head or distance change (face-size spread above 12 % or head angle spread above 20 degrees across the dots), else high error; a point without a usable frame counts as a failure; with fewer than 2 usable frames in total there is no model and the gate reports few frames. The model of the last passed attempt (else the last attempt) is kept; later validation blocks are scored with it and logged with attempt 1.
  8. **Session plan:** the `calibration` step now includes the pre-video validation and the gate with its retries; `validation` steps are only the blocks between and after videos. A study with `on_fail: end_session` ends after the last failed attempt: the participant sees a thank-you text and the recording is uploaded.
  9. **Fullscreen in dots:** an interrupted dot restarts with the same `id` after fullscreen returns (two shows, the first closed by a hide).
  10. **Debug mode:** the panel is on by default and hidden with `D` (it covers where dots appear, found by the e2e test); `S` ends the running dot block or video (video: logs `video_ended`); `F` toggles the forced failure; the sliders write into the session's own `quality` config object, so the device check and the gate use them at once; the gaze dot is the mean of the last 5 predictions of the quick model.
  11. **Moving-dot fixture** (§12.4): `e2e/fixtures/moving_dot.mp4`, 1280x720, 25 fps, 21.8 s (545 frames, 9 waypoints with 1.5 s pauses and 0.7 s smoothstep moves, a 2 s first pause), with `moving_dot_path.json` (per-frame ground truth, computed by the code that draws the frames; dot centroid within 0.23 px). Not used by the e2e test (it plays the 3 s test clip); for the pilot checklist.
  12. **e2e:** the fake camera plays `e2e/fixtures/face.mp4` as `.y4m`; it cannot follow the dots, so the e2e studies use `calibration_gate.max_attempts: 1` and the test asserts that the `calibration_gate_failed` flag is present exactly when the logged pre-video result failed.
- Reason: needed to build the tasks; nothing here contradicts the plan except the points marked as additions.
- Approved by: plan leaves open (Nágó Dániel approved starting Milestone 2 tasks without review; check-in pending)

## 2026-10-15 — Milestone 2 T3: feature definition and the face fixture
- Plan section: §5.4, §5.6 step 2, §8.4
- Decision:
  1. **Definitions** (§8.4 leaves details open; full text in the docstring of `backend/worker/pipeline/features_a.py` and the header of `frontend/src/face/features.ts`): landmarks are scaled to `(x * aspect, y)` first (the functions take the frame's aspect ratio); iris centre = mean of the 5 iris landmarks in the eye-local frame (origin: midpoint of the corners, x-axis corner to corner pointing image-right for both eyes, y-axis towards the image bottom, unit: eye width); openness = distance of the middle lid landmarks (right 159/145, left 386/374) over eye width; face scale `face_ratio` = distance of the two eye midpoints over the frame width; head angles in degrees from `R = Rz(roll) Ry(yaw) Rx(pitch)` (positive pitch: head down, positive yaw: face turned to the image right, positive roll: counter-clockwise), translation in cm.
  2. **`matrix` layout in feature records: 16 numbers, row-major** (translation at indices 3, 7, 11), as MediaPipe's Python API returns it. The browser API's layout is checked against the same clip in T6 and converted if needed.
  3. **Blink rule not in T3** (the openness threshold of §8.4 is "tuned on calibration data"; the fixture shows blinks at openness 0.07 to 0.14 against 0.3 to 0.39 with eyes open): it comes with the gate in T4/T10.
  4. **Face clip fixture:** `e2e/fixtures/face.mp4` (first 12 s of the human's clip, 854x480, 30 fps, no audio, 229 KB; the 1280x720 original with sound stays untracked as `face_test.mov`). 854x480 keeps the 16:9 shape and meets the 640x480 camera minimum of §5.2. `shared/fixtures/face_frames.json` holds 45 real MediaPipe frames (landmark subset, matrix, blink scores) and the features the Python code computes; `scripts/make_face_fixture.py` regenerates it. Both languages are tested on it (agreement far below the 1e-4 of §5.6).
  5. **Corrects T1 item 6:** the real clip has a face ratio of 0.070 at 44 cm from a 16:9 laptop camera, so the starting band is `face_ratio_min` 0.045 and `face_ratio_max` 0.16 (was 0.07 and 0.2, which would have rejected normal arm's-length use). Still a starting value for the pilot.
  6. The frontend imports `shared/landmark_subset.json`: the dev server may serve `..` (`server.fs.allow`), the proxy image build context includes the file and the test fixture, and the backend image carries `/shared/landmark_subset.json` (same layout as the repository, checked in `make test-images`).
- Reason: plan leaves the exact definitions open; the numbers come from real data.
- Approved by: plan leaves open (flag if you object); the clip use was approved by the human

## 2026-10-15 — Milestone 2 T2: backend for the new streams
- Plan section: §5.4, §5.6, §6, §7, §12.1
- Decision:
  1. **Debug authorization is decided by the server alone** (refines item 6 of "Milestone 2 scope"): `POST /api/sessions` with `debug: true` is accepted when the study has `debug_allowed` or the request carries a valid admin session cookie (same origin, so the browser sends it); otherwise 403 and no session is created. The participant app does not call `/admin/api/me`.
  2. **Validation at the PUT:** `features` and `stimulus_frames` chunks must be valid gzip within `MAX_EVENTS_CHUNK_INFLATED_MB` (the same setting as `events`; its name is kept so `.env` files stay valid), else 422 and nothing is stored. The first webcam chunk (seq 0) must start with the WebM (EBML) magic bytes `1A 45 DF A3`, else 422. Other webcam chunks are not inspected.
  3. **At `/complete`:** `raw/browser_features.jsonl` and `raw/stimulus_frames.jsonl` are written when the stream has chunks (gunzip and concatenate, every line validated as `FeatureRecord` / `StimulusFrameRecord`; about 0.6 s for 10 minutes of features at 30 fps). A bad chunk or line writes nothing for that stream, the session still completes, and it gets the flag `features_invalid` or `stimulus_frames_invalid` (like `events_invalid`). A session without such chunks gets no flag here.
  4. **`calibration_gate_failed`** is set when the last `before_video` `calib_gate_result` of the events has `passed: false`; skipped results and later blocks do not count. A `calib_gate_result` of the wrong shape counts as an invalid events stream (`events_invalid`).
  5. `webcam.webm` (concatenate and remux) is not produced by the server in M2 (M3 ingest).
- Reason: details the plan leaves open; keep the participant's upload from ever blocking on a stream problem (§1: flag the session and move on).
- Approved by: plan leaves open

## 2026-10-15 — Milestone 2 T1: stream records and event data (choices the plan leaves open)
- Plan section: §4.1, §5.3, §5.4, §5.6, §5.9, §12.1
- Decision:
  1. **Landmark subset** (`shared/landmark_subset.json`): 49 MediaPipe mesh indices in named groups (both iris rings 468-477, both eyes' corners and upper/lower lids, nose tip 1, chin 152, forehead 10, cheeks 234/454/205/425). `lm` holds the groups in file order. Checked against real MediaPipe output in T3; the list may change until then.
  2. **Feature record without a face:** `face: false` and every measurement null. The webcam-frame values of §5.3 (`captureTime?`, `presentedFrames`) are two extra fields of the feature record (`capture_time`, `presented_frames`), because the plan logs them "at every frame where the face model ran" but names no stream.
  3. **Stimulus-frame record keys are snake_case** (`expected_display_time`, `media_time`, `presented_frames`), like the rest of our JSON, not the camelCase of the browser API. The record has no video id; the `video_*` events delimit the videos.
  4. **Camera settings and mime type** (§5.9 puts them in `session_start`) are logged in `recorder_start` instead, because the camera starts after `session_start`.
  5. **`calib_gate_result` data** (§5.6) adds `block` (`before_video`, `between_videos`, `after_last_video`), `skipped` (`passed` is null exactly then) and a fixed `reason` list; the participant-facing message is chosen from it. Rule for T2: `calibration_gate_failed` is set when the last `before_video` result has `passed: false`.
  6. **Quality keys** under `quality` with starting values: `brightness_min` 0.25, `brightness_max` 0.85, `face_ratio_min` 0.07, `face_ratio_max` 0.2 (interocular distance over frame width at roughly arm's length; the plan's own example is 0.162; corrected in T3, item 5), `max_yaw_deg` 20, `max_pitch_deg` 20. Not from a published source; tune on the pilot (debug-mode sliders, T11). Lower bounds must stay below upper bounds.
  7. Other new event data: `recorder_start {mime_type, bits_per_second, settings}`, `recorder_dataavailable {seq, size}`, `recorder_stop {chunks, bytes}`, `face_lost {}`, `face_regained {lost_ms}`, `debug_skip {target}`. The server does not validate event data (as in 1b), except `calib_gate_result` from T2 on.
- Reason: the plan names these records and events without exact shapes; both sides need one definition (§1 single source of truth).
- Approved by: plan leaves open (item 3 and the snake_case naming: flag if you object)

## 2026-10-15 — Milestone 2 scope and open choices
- Plan section: §5.2, §5.3, §5.4, §5.6, §8.4, §12.1, §12.4, §13, §14
- Decision:
  1. **Face fixture.** Nágó Dániel records a short clip of his own face (about 6 to 10 s) for the fake-camera e2e test and the feature fixtures; a small MP4 is committed, `e2e.sh` converts it to `.y4m`.
  2. **Python features.** The pure function `backend/worker/pipeline/features_a.py` is written in M2 (needed for the TS/Python agreement test of §5.6 step 2); M3 reuses it.
  3. **Quality thresholds.** The plan gives numbers only for `min_face_ratio_warn` and `max_face_ratio_warn`. Brightness bounds, the face-size band for the device check and the frontal-head limits are new keys under `quality` in `StudyConfig` with starting values chosen by the implementer from common practice (not from a published algorithm; none exists for this). They are tuned on the pilot. To make tuning easy, debug mode (§12.1) gets live sliders for these thresholds next to the measured values, and a "copy as JSON" button that prints a `quality` block for the study config. Assumption to confirm in T1: `min/max_face_ratio_warn` are multipliers of the median face ratio measured in the 3 s device check.
  4. **Face model placement.** Web Worker first; main thread if MediaPipe does not run reliably in a worker. CPU delegate only (headless CI has no GPU).
  5. **Gate flag.** The server derives `calibration_gate_failed` from the `calib_gate_result` events at `/complete`; the client sends nothing extra.
  6. **Debug authorization.** `POST /api/sessions` accepts `debug: true` only if the study has `debug_allowed` or an admin is logged in on that browser (`/admin/api/me`); otherwise 403. Debug sessions are listed normally in the admin pages.
  7. **Webcam assembly.** `/complete` also writes `raw/browser_features.jsonl` and `raw/stimulus_frames.jsonl`. Concatenating and remuxing `webcam.webm` stays in the M3 ingest; the e2e test checks the chunk concatenation with ffprobe.
  8. **Gate without validation.** A study with `calibration_gate.enabled` and `validation.before_video: false` is not refused. The gate cannot be scored, so it is skipped and logged as a `calib_gate_result` with `skipped: true` and the reason.
  9. **Fullscreen exit during calibration/validation.** The same "Return to fullscreen to continue" gate as during playback blocks the dots until re-entry; the exit is logged.
  10. **Face model rate (deviation from "about 15 fps", §5.4).** The target face model rate is the stimulus video's fps (capped at the camera's actual fps) during playback, and the camera's fps during calibration and validation, instead of a fixed 15. The playback-smoothness rule stays: if the stimulus drops frames, the rate is reduced (named constants for the window, the dropped share, the minimum rate and the recovery, tuned on real laptops).
  11. **IndexedDB growth.** No cap on pending webcam bytes; the debug panel shows them and a storage error fails loudly (existing behaviour).
  12. **No split.** Milestone 2 stays one milestone with the 14 tasks.
- Reason: answers to the planning questions of `/milestone-start 2`.
- Approved by: Nágó Dániel (item 10 was his suggestion; item 3 assumption still open)

## 2026-10-14 — Milestone 1b review decisions
- Plan section: §1, §5.8, §6, §10
- Decision: results of `/milestone-review 1b`.
  1. **Participant codes in logs stay (review finding B2 not acted on).** The client-error report logs the page URL, which contains `?p=<code>`, and the access log writes admin paths such as `/admin/participants/{study}/{code}`. Decided by Nágó Dániel: no stripping or redaction. PLAN §10 forbids logging raw media and landmark data, not codes. Consequence to keep in mind: the container logs (rotated at 20 MB x 5) contain participant codes, which are derived from personal data (see the participant code entry).
  2. **A session never gets stuck on its events.** `/complete` finishes the session (`uploaded`) even if the events stream cannot be assembled (an unknown event type from a newer frontend, an invalid line), adds the flag `events_invalid`, writes no `raw/events.jsonl`, and logs the reason. This replaces the 422 of the earlier choice (item 5 of "implementation choices", and DECISIONS 1b scope item 3: `events.jsonl` is only written for a valid stream). Approved by Nágó Dániel.
  3. **Events chunks are checked when they are stored.** A chunk of the `events` stream must be one complete gzip stream that inflates to at most `MAX_EVENTS_CHUNK_INFLATED_MB` (default 32, named setting); otherwise `PUT` answers 422 and nothing is stored. Event types and lines are not checked at the PUT (see item 2). The same limit applies when the admin pages and `/complete` decode the chunks (a few KB of gzip can inflate to gigabytes).
  4. **A session with no chunks completes and is flagged `empty`** (a participant who reloads in the first `chunk_ms` and resumes ends up here); the flags in use are `interrupted`, `events_invalid` and `empty`. Recommended by the agent, approved by Nágó Dániel by delegation ("continue with the rest").
  5. **Client-error reports have their own rate-limit bucket** (same limit, `RATE_LIMIT_PER_HOUR`), so an error loop cannot use up the session creations behind a shared address. Same approval as item 4.
  6. **`interrupted` in the `/complete` body (addition to PLAN §6).** `CompleteRequest` has an optional boolean `interrupted`; the participant app sets it when it uploads the leftovers of a reloaded session (PLAN §5.8: "flagged as interrupted"); the server adds the flag `interrupted`. The plan names the flag but no way to send it.
  7. **The point counts are validated when a study is saved.** `calibration.points` must be 13, `validation.points` must be 5 and `calibration.margin` must be below 0.25, because the participant app supports exactly these (PLAN §5.5); the values are in `shared/protocol.json` and compared on both sides. Before this, a study that no participant could run could be saved.
  8. **Database connections are released after authentication** (before a request body is read), so stalled uploads cannot use up the pool (5 + 10 per process).
  9. **The participant link follows the scheme of the request** (`X-Forwarded-Proto` from the proxy), so it is `http://` in the dev stack.
  10. **Known and accepted for now (Milestone 6 hardening):** there is no global disk quota (anyone with a study code can create 300 sessions per hour per address, 2 GiB each); the rate limiters are per API process (the effective limit is multiplied by `API_WORKERS`); consent compares the version with the study's current config, so an admin edit between page load and consent gives a 409 on the consent screen.
- Reason: findings of the plan-reviewer, decisions of the human.
- Approved by: Nágó Dániel (items 1, 2); items 3 to 9 recommended by the agent in the review and approved by "continue with the rest"; item 10 recorded

---

## 2026-10-14 — Admin additions: video preview and per-participant results page
- Plan section: §6 (admin), §10, §12.2, §14
- Decision: two admin features are added to the plan's scope; `docs/PLAN.md` itself is not edited (project rule), this entry is the record.
  1. **Stimulus video preview.** The admin page of a video plays the uploaded stimulus video (`<video controls>` on `/media/videos/{id}.mp4`) so the researcher can check what was uploaded. Milestone 1b, task T16.
  2. **Participant results page.** A page for one participant code in one study (`/admin/participants/{study_id}/{code}`, JSON at `/admin/api/participants/{study_id}/{code}`) listing all of that participant's sessions (status, flags, chunk counts) and a summary derived from the events (calibration and validation dots clicked, per video: played, ended, watch time, pauses and stalls, fullscreen exits), with links to the session detail. Reached from the sessions list. Milestone 1b, task T17. Later milestones add their results to this same page instead of creating new ones: quality report and plots (M3, with the session inspector of §12.2), heatmaps, AOI metrics and synchrony for this participant (M4), Pipeline B and the comparison (M5).
  3. **Webcam recording playback (later).** Playing a participant's recorded webcam video in the admin is part of the Milestone 3 session inspector (§12.2 already has the debug replay). It is only served to logged-in admins (§10), is shown on request, and is labelled as containing participant video. It is not in 1b because the webcam is recorded from Milestone 2.
- Reason: requested by the human on 2026-10-14. The stimulus videos are served to every participant by design (the proxy route is public), so a preview exposes nothing new; participant results are admin-only, like all other admin pages.
- Approved by: Nágó Dániel

---

## 2026-10-13 — Milestone 1b implementation choices (found while building T4-T13)
- Plan section: §4.1, §5.2, §5.5, §5.7, §5.8, §6, §10
- Decision: choices the plan leaves open, made by the implementing agents with the simplest option and reviewed at checkpoint 1.
  - **Participant API (T6-T8).** (1) Session endpoints compare the consent version with the study's current config, not the session snapshot. (2) `PUT` chunks before consent, or after `/complete`, give 409; `seq` is limited to 2^31-1 (the DB column). (3) `store_chunk` inserts the `chunks` row first, then writes the file, then commits, so two racing PUTs of the same seq with different bytes cannot overwrite the winner; a crash in between leaves an orphan file that the next PUT overwrites. (4) The per-session total check can overshoot by up to one chunk under concurrency. (5) `/complete` validates every event line with `parse_events`: one bad line or unknown event type gives 422 and the session cannot complete (fail loudly). (6) New named settings: `MAX_SESSION_TOTAL_MB` 2048, `CLIENT_ERROR_MAX_BYTES` 16384, `RATE_LIMIT_PER_HOUR` 300, `UPLOAD_TOKEN_MAX_AGE_S` 86400, `MAX_CHUNKS_PER_STREAM` 100000 (a sanity cap on `last_seq` in `/complete`, so a token holder cannot ask for a huge `missing` list; added by the coordinator). The rate-limit window (3600 s) is a module constant. (7) The client IP for rate limiting is the rightmost `X-Forwarded-For` entry (Caddy, the only way to reach the api, sets it), falling back to the socket address; the limiter is per API process, so with `API_WORKERS=2` the effective limit is twice the setting. (8) The client-error log line names the message field `error_message` because `logging` reserves `message`. (9) `raw/meta.json` carries more than the contract lists (consent fields, `is_debug`, `flags`, `client_info`), empty at creation and filled in at `/complete`; the config snapshot is under `config`.
  - **Admin (T4, T5, T9).** (1) Study codes must match `^[A-Za-z0-9_-]{3,32}$`; a study's code may be changed (the participant link changes, the page says so). (2) Admin pages use plain HTML forms with a hidden `csrf_token` and no JavaScript, because the CSP forbids inline scripts; anonymous page requests get 303 to the login, the JSON API gets 401; an unknown id on a page gives the JSON 404. (3) Video upload is multipart; Starlette spools the body to a temporary file before it is copied, so disk use is up to twice the file size during an upload, and the size limit is exact only during the copy (a `Content-Length` pre-check and the proxy cap guard the rest). A true streaming parser would avoid the double write; not done (YAGNI). (4) Variable frame rate is detected when `r_frame_rate` and `avg_frame_rate` differ by more than 1 % (`VFR_RELATIVE_TOLERANCE`); a non-MP4 container also warns. (5) Deleting a video is refused with 409 when any session `meta.json` cannot be read, because use cannot be ruled out. (6) The session detail shows at most 1000 events (`MAX_EVENTS_SHOWN`) plus the total, and shows an `events_error` instead of failing when the chunks cannot be decoded.
  - **Frontend (T10-T13).** (1) The state machine is in `src/flow.ts` (`runApp`), `main.ts` only mounts it. (2) The event log, upload queue and `session_start` start after consent succeeds; events before consent are not logged, and a reload within one `chunk_ms` interval loses the unflushed events (only stored chunks are resumed). (3) A click on a calibration dot is accepted from `dot_duration_ms - 600` ms onwards and stays acceptable afterwards, so a slow participant is not stuck (the plan says "accepted only in the last 600 ms" and does not say what happens after that; this is a reading of it); after an accepted click the dot stays 100 ms before `calib_dot_hide`. (4) Dot `id` is the index in shown order, restarting at 0 per block; validation dots use `calibration.dot_duration_ms` (no separate field). (5) `viewport_change.video_rect` is the letterboxed content rectangle (§5.7), not `getBoundingClientRect()` (§5.5); PLAN §5.5 and §5.7 contradict each other on this, and §5.7 (computed from the intrinsic size) is the one that is useful for mapping gaze; it is also logged when a video appears or disappears. (6) The browser check also requires `CompressionStream` and `crypto.subtle` (the upload needs them) and a WebM codec for `MediaRecorder`; an unknown requirement name from the server counts as missing. (7) `finish()` re-sends only chunks still in IndexedDB; a chunk deleted after a 2xx that the server later reports missing is a loud error, not a silent skip (PLAN §5.8 deletes after 2xx and also says to re-send). (8) A fatal upload error (409, 401, ...) ends the session on the error screen; Resume offers "Resume upload" or "Discard"; a second tab would offer to resume the first tab's session (not handled). (9) A video is held in memory while preloading (chunks, Blob, and once as an `ArrayBuffer` for hashing); not measured for large files. (10) The seek guard resets `currentTime` when it jumps more than 0.5 s (`SEEK_TOLERANCE_S`). (11) Calibration and validation require exactly 13 and 5 points and throw otherwise.
- Reason: the plan is silent on these; each is the simplest option that satisfies the plan.
- Approved by: plan leaves open (agent choices, reviewed by the coordinator at checkpoint 1). PLAN §5.5 puts the 4 calibration quadrant centres and the 4 validation points at the same positions (0.25/0.75), so validation measures error at points the fit was trained on. Implemented literally. Decided 2026-10-14 by Nágó Dániel: keep the validation points as in the plan (centre and the four inner corners at 25 % / 75 %). Note: the human described them as "corners and top middle"; the code has no top-middle validation point (the calibration grid has one), so this was recorded as "keep as written"; a different layout would be a new deviation.

---

## 2026-10-12 — Milestone 1b scope and open choices
- Plan section: §4, §4.1, §5.2, §5.4, §5.8, §5.9, §6, §7, §8.1, §10, §11.5, §11.6, §12.1, §14
- Decision:
  1. **Stations (deviation from §14, §5.2):** no station CRUD and no station picker in 1b; the project has one station. The `stations` table and `sessions.station_id` stay unused.
  2. **No job on `/complete` until M3 (deviation from §6):** `/complete` sets `uploaded` but does not enqueue `process_session`, because the worker has no handler yet.
  3. **`events.jsonl` assembled at `/complete` (ahead of §8.1):** the API gunzips and concatenates the events chunks into `raw/events.jsonl` with one pure function that the M3 ingest stage reuses.
  4. **Status `recording`:** set when the first chunk of a session is accepted (the plan names the status but no trigger).
  5. **Events chunks:** gzip per chunk like `features`, flushed every `recording.chunk_ms`; an empty interval produces no chunk; sha256 is over the compressed bytes.
  6. **Consent text:** plain-text files `backend/app/consent/<consent_version>.txt` in the backend image; a study config naming a version without a file is rejected.
  7. **`meta.json`:** written at session creation with the config snapshot, rewritten atomically at `/complete` with the final session row and `client_info`; the config part never changes afterwards.
  8. **Videos:** non-H.264 or variable frame rate gives a warning only; a video referenced by a study playlist or a session snapshot cannot be deleted; no replace. Playlist entries are video ids as strings.
  9. **Limits:** per-session total is a global setting `MAX_SESSION_TOTAL_MB` (default 2048). Session creation and client-error reports are limited to 300 per IP per hour (named setting, in-memory per API process), high enough to stop only bots; a class behind one address must not be blocked.
  10. **`POST /api/client-errors`:** no authentication, optional `session_id`, body `{message, stack, url, t}`, 16 KiB cap, structured log only (no table).
  11. **Participant code format:** two ASCII letters + birth month + birth day, `^[A-Z]{2}(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])$` (first two letters of the name; input upper-cased). The agent pointed out that this is derived from personal data and can collide; kept by the human for now ("we'll see later").
  12. **Sessions in admin:** list and detail only; no create, edit or delete (withdrawal is M6).
  13. **Shared definitions:** stream names, event types and the other shared values live in `shared/protocol.json`. Python and TypeScript mirror them in their own code (as PLAN §5.9 says: defined in `eventLog.ts`, mirrored in `schemas.py`), and a test on each side compares its copy with the JSON; the JSON is the reference, it is not read at run time. (Corrected 2026-10-14: this item first said "read by".)
  14. **Playwright E2E in 1b (replaces "E2E added in M2"):** a camera-less end-to-end test runs in 1b; the fake-camera version follows in M2.
  15. **1b boundary:** playback events and the content rectangle are in 1b; the `stimulus_frames` stream, all camera work, the calibration gate and debug mode are M2. The device-check screen is a placeholder.
  16. **§11.6 fresh-VM re-run for 1b:** postponed by the human.
  17. **Carried 1a review items (R5):** recorded as accepted deviations without code change: `WORKER_CONCURRENCY` supports only 1 (§11.1); `create-admin` requires `--username` (§11.3); `make test` and `make lint` run on the host through uv and npm, not through compose (§11.5); log lines omit `session_id`/`job_id`/`stage` when they are not bound (§12.3); `setup.sh` does not offer `fetch_models.sh` (§11.3, revisit in M5). Fixed in 1b: `/admin` without a trailing slash, `SECRET_KEY` minimum length 32.
- Reason: answers to the numbered questions of `/milestone-start 1b`; the plan is silent or contradictory on these points.
- Approved by: Nágó Dániel (items 5, 6, 7, 9 values, 10 were proposed by the agent and accepted)

## 2026-10-12 — Milestone 1a accepted; stale `running` jobs deferred to Milestone 3
- Plan section: §4, §11.4, §14
- Decision: Milestone 1a is accepted. A job whose worker is killed mid-flight (for example by `docker compose up -d --build`) stays in status `running` and is never picked up again; this is not handled in 1a or 1b, because no job handlers exist before Milestone 3. Milestone 3 must decide the semantics (requeue on worker start, or mark `failed` with a clear log line), implement it with a test on real Postgres, and test `worker.run.main()` (SIGTERM, concurrency guard). Review tasks R5 (minor unrecorded deviations) and R6 (test and CI gaps) move into the Milestone 1b task list.
- Reason: found in the 1a review (R2); no effect until real jobs run.
- Approved by: Nágó Dániel

## 2026-10-12 — Milestone 1a review: admin body cap, and deviations re-labelled
- Plan section: §10, §11, §11.1, §11.2, §11.6
- Decision: (1) New setting `MAX_ADMIN_REQUEST_MB` (default 1, MiB): the proxy caps the body of every `/admin/*` request except the video upload path (`/admin/api/videos`, which keeps `MAX_VIDEO_UPLOAD_MB`). This covers the unauthenticated `POST /admin/login`. It replaces "other `/admin/*` routes have no proxy cap" in the T9 entry. Login rate limiting / lockout is still not implemented; it is planned for Milestone 6 (hardening), until then each login attempt costs one argon2 verification. (2) Three items that earlier entries marked "plan leaves open" are in fact deviations from plan text and are recorded as such: `migrate` runs as root to create and chown the data directories, then drops to uid 10001 (§11.1: backend image "runs as a non-root user"); `migrate` has `restart: "no"` and no healthcheck (§11.1: "Every service: `restart: unless-stopped`, a healthcheck"); the acceptance run used Ubuntu 26.04 ARM64 with 2 vCPU / 3.7 GB (§11 names Ubuntu 22.04/24.04; CI runs the full `setup.sh` on ubuntu-24.04 with internal TLS).
- Reason: found by the `plan-reviewer` in `/milestone-review 1a`. A one-shot service cannot be `unless-stopped`, and Docker creates bind-mount directories root-owned, so something must chown them.
- Approved by: Nágó Dániel (2026-10-12, approved review tasks R1 and R3)

## 2026-10-12 — scripts/install-docker.sh (addition after T13)
- Plan section: §11, §11.3
- Decision: added `scripts/install-docker.sh` for Ubuntu and Debian: installs Docker Engine, CLI, containerd, buildx and the Compose plugin from `download.docker.com` (key fetched over HTTPS, as in Docker's guide; no extra fingerprint pinning), checks that Docker publishes packages for the release codename, re-runs itself with sudo, is idempotent (does nothing when Docker and Compose are present), and offers `--add-user`. `setup.sh` points to it when Docker is missing. Tested for real in Ubuntu 24.04 and Debian 12 containers (`make test-install-docker`, also a CI job); the same steps ran on the Ubuntu 26.04 ARM acceptance server.
- Reason: the plan treats Docker as a prerequisite, but a new server (for example after deleting a test VPS) needs it, and doing it by hand is error-prone.
- Approved by: Nágó Dániel (requested)

## 2026-10-12 — HTTP/3 disabled; acceptance platform (T13)
- Plan section: §11.1, §11.4, §11.6
- Decision: the proxy serves HTTP/1.1 and HTTP/2 only (`servers { protocols h1 h2 }`), because the plan publishes only 80 and 443/tcp and Caddy would otherwise advertise HTTP/3 (`alt-svc`) on UDP 443 that is not reachable. The acceptance run (§11.6) used a Hetzner Cloud VPS: Ubuntu 26.04.1 LTS, aarch64, 2 vCPU, 3.7 GB RAM, Docker 29.9.0, Compose 5.6.0; this is outside the plan's named Ubuntu 22.04/24.04 and below its sizing advice (4+ cores, 16 GB), which the README states.
- Reason: avoid advertising an unreachable protocol; record what the acceptance test actually ran on.
- Approved by: HTTP/3 plan leaves open; acceptance platform is a deviation from §11, approved by Nágó Dániel in the 1a review (see the review entry above)

## 2026-10-12 — ACME_EMAIL is optional (corrects T8/T11)
- Plan section: §11.2
- Decision: with `TLS_MODE=acme` the contact e-mail is optional. Empty, or the example value `you@example.org`, means "request the certificate without a contact address" (Caddy's automatic HTTPS needs none; the address only receives expiry reminders). A given address must look like an e-mail (no whitespace, braces or quotes, since it is written into the Caddy config). `proxy/entrypoint.sh` and `scripts/setup.sh` implement this and tests cover empty, example, valid and invalid values.
- Reason: T8/T11 wrongly made it mandatory and rejected the example value; the plan does not require it. Pointed out by Nágó Dániel.
- Approved by: Nágó Dániel

## 2026-10-12 — Backup, restore, model download and CI details (T12)
- Plan section: §11.2, §11.4, §11.5
- Decision: (1) `shared/models.json` gains a `file` name per model; `shared/download_model.py fetch DIR NAME...` writes `DIR/<file>`, skips files whose sha256 already matches, replaces corrupted ones, never touches an existing file when a download fails, and retries network errors 3 times (60 s timeout, 5 s delay; integrity errors are never retried). `scripts/fetch_models.sh` runs it in a throw-away `python:3.11-slim` container (the host needs only Docker); default model `l2cs_resnet50`, optional `l2cs_mobileone_s0`. (2) `scripts/backup.sh TARGET` needs the stack running and writes `eyetrack-backup-<UTC timestamp>/` (mode 700; files 600) with `db.dump` (pg_dump custom format), `data.tar.gz` (streamed out of the api container, so DATA_DIR may be anywhere and root-owned) and `MANIFEST.txt` (raw-video flag, git hash, study/video/session/file counts, sha256 of both files). `--no-raw-video` excludes `*/raw/webcam.webm` and `*/raw/chunks/webcam`. `.env` is deliberately not backed up. Written to a `.partial` directory and renamed, so a failed backup leaves nothing. tar exit code 1 (file changed while reading) is a warning; an empty or unreadable archive is an error. (3) `scripts/restore.sh BACKUP_DIR` verifies both checksums before touching anything, refuses a non-empty target (studies/videos/sessions rows or data files) unless `--force`, asks the user to type the project name unless `--yes`, stops api/worker/proxy, restores the database in one transaction (`pg_restore --single-transaction --clean --if-exists`), extracts the data, restarts the stack (so migrations run if the backup is older than the code), and compares row counts with the manifest. `--force` replaces database content and overwrites same-named files but does not delete other files. (4) CI (`.github/workflows/ci.yml`): jobs `checks` (make lint, make test), `images`, `stack` (stack, setup, backup tests), `dev-stack` (dev stack, model download); GitHub actions are pinned to major versions, not commit SHAs, which should be revisited before real data is handled. Playwright E2E joins the `stack` job in Milestone 2.
- Reason: plan lists the scripts and CI jobs but not these details.
- Approved by: plan leaves open

## 2026-10-12 — setup.sh design (T11)
- Plan section: §11.3
- Decision: `scripts/setup.sh` checks Docker >= 24.0 and Compose >= 2.24 (named values `SETUP_MIN_DOCKER`/`SETUP_MIN_COMPOSE`; Compose 2.24 for `depends_on` conditions with `--wait`), creates `.env` from `.env.example` if missing (mode 600), prompts for DOMAIN / TLS_MODE / ACME_EMAIL or certificate paths, replaces only `change-me` secrets with random 40-character values, validates the config, checks DNS for `acme`, runs `docker compose up -d --build --wait`, and creates the first admin. DNS check compares the A records of DOMAIN with this machine's local IPv4 addresses (no external service is contacted); a mismatch asks for confirmation interactively and fails in `--non-interactive` mode unless `--skip-dns-check` is given (servers behind NAT). It refuses to generate a new `POSTGRES_PASSWORD` when a `<project>_pgdata` volume already exists, because the database keeps its original password. Extra flags beyond the plan: `--non-interactive`, `--env-file`, `--project`, `--dry-run`, `--env-only` (create `.env` with secrets without Docker, used by `make dev`), `--admin-username`, `--admin-password-stdin`, `--no-admin`. The admin password is only ever passed over stdin, never on a command line. Re-running is idempotent (an existing admin is reported and left unchanged). Uses only POSIX tools plus bash 3.2-compatible syntax, no Python on the host.
- Reason: plan lists the steps but not the details; the flags make the script testable and scriptable.
- Approved by: plan leaves open

## 2026-10-12 — Development stack design (T10)
- Plan section: §11.5, §1 (single source of truth)
- Decision: `compose.dev.yml` keeps the production services and replaces the proxy with plain `caddy:2` running `Caddyfile.dev` (HTTP only on `127.0.0.1:${DEV_HTTP_PORT:-8080}`, `auto_https off`), which reverse-proxies everything else to a new `web` service (node:20, Vite dev server with the frontend mounted, HMR over the same port). Routes for `/api`, `/admin` (incl. the video-upload cap) and `/media/videos` are defined once in `proxy/routes.caddy` and imported by both `Caddyfile` and `Caddyfile.dev`, so dev and prod cannot drift. `api` runs `uvicorn --reload` with `backend/app` mounted read-only; `worker` mounts the same source (no auto-reload, restart it by hand); `backend/alembic` is mounted so new migrations need no rebuild. Postgres is published on `127.0.0.1:${DEV_DB_PORT:-5432}` only. `ADMIN_COOKIE_SECURE=false` in dev. `make dev` stops with instructions if `.env` is missing (it never creates or edits `.env`); secrets must be real values because the backend rejects `change-me`; `scripts/setup.sh` (T11) will generate them.
- Reason: plan describes the dev override in one sentence; a separate dev proxy keeps same-origin cookies, video range serving and the camera secure-context rule without TLS.
- Approved by: plan leaves open

## 2026-10-12 — Compose stack details and admin video upload cap (T9)
- Plan section: §6, §11.1, §11.2
- Decision: (1) Admin stimulus-video uploads are capped by the proxy with the new setting `MAX_VIDEO_UPLOAD_MB` (default 2048) on `POST /admin/api/videos` and `/admin/api/videos/*`; the 1b upload endpoint must use exactly that path prefix, stream to disk, and enforce the same limit. Other `/admin/*` routes have no proxy cap. Approved by Nágó Dániel (replaces open item 4 of the T8 entry). (2) `migrate` runs as root only to create `/data/{videos,studies,analysis}` and chown them to uid 10001 (Docker creates bind mounts root-owned), then drops privileges with `setpriv` and runs `alembic upgrade head`; this keeps the plan's five services and non-root app containers. (3) `migrate` has `restart: "no"` (plan says every service `unless-stopped`; a one-shot that exits cannot). (4) Added `.env` keys `LOG_LEVEL`, `MAX_VIDEO_UPLOAD_MB`, `HTTP_PORT`, `HTTPS_PORT` (ports only for testing; production keeps 80/443). `TLS_CERT_FILE`/`TLS_KEY_FILE` are host paths, bind-mounted read-only into the proxy at `/run/tls/`; unset ones mount `/dev/null` and the proxy entrypoint rejects empty files in `files` mode. (5) Caddy state lives in volumes `caddy_data`/`caddy_config` at `/caddy/*` (Caddy's default `/data` would collide with the video mount), so certificates persist across restarts. (6) Proxy healthcheck is `nc -z 127.0.0.1 443`. (7) Postgres user and database are fixed to `eyetrack`, matching the backend defaults. (8) No `gpu` profile (deferred to M5, see earlier entry).
- Reason: plan lists the services and `.env` keys but not these details.
- Approved by: (1) Nágó Dániel; (2) and (3) are deviations from §11.1, approved by Nágó Dániel in the 1a review (see the review entry above); rest plan leaves open

## 2026-10-12 — Proxy design and shared model manifest (T8)
- Plan section: §1 (single source of truth), §3, §11.1, §11.2, §10
- Decision: (1) Model URLs, sizes and sha256 live only in `shared/models.json` (face_landmarker, plus the two L2CS weights from the earlier decision). It is read by `shared/download_model.py` (backend image build, T12 `fetch_models.sh`) and `frontend/scripts/fetch-mediapipe.mjs` (proxy image build). This replaces the Dockerfile `ARG` pin from T7; the backend image's build context is now the repo root (`docker build -f backend/Dockerfile .`). (2) Browser MediaPipe files are served from `/mediapipe/wasm/*` and `/mediapipe/face_landmarker.task` (generated into `frontend/public/mediapipe/`, gitignored). (3) `Caddyfile` at repo root; `proxy/entrypoint.sh` writes the TLS directive for `TLS_MODE` and fails loudly on a bad combination. (4) The proxy request-body limit (`MAX_UPLOAD_CHUNK_MB`, MiB) applies to `/api/*` only. Admin routes: see the T9 entry (video upload cap `MAX_VIDEO_UPLOAD_MB`). (5) CSP: `script-src 'self' 'wasm-unsafe-eval'`, `style-src 'self' 'unsafe-inline'` (admin templates use an inline `<style>`), `worker-src 'self' blob:`. (6) Video URL `/media/videos/{numeric id}.mp4` maps to `/data/videos/{id}/video.mp4`; any other `/media/*` path is 404.
- Reason: plan requires single source of truth; it says the proxy enforces `MAX_UPLOAD_CHUNK_MB` but is silent on admin video uploads.
- Approved by: plan leaves open

## 2026-10-12 — Backend image: MediaPipe model pin and extra system libraries (T7)
- Plan section: §11.1
- Decision: `face_landmarker.task` pinned to https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task, 3,758,596 bytes, sha256 `64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff` (downloaded 2026-10-12). Verified at build (now via `shared/models.json`, see the T8 entry); stored at `/opt/mediapipe/face_landmarker.task` (not under `/models`, which is a bind mount). Extra apt packages beyond the plan's `libgl1`/`libglib2.0-0`: `libegl1` and `libgles2`, because mediapipe 1.1.0's `libmediapipe.so` fails to load without `libEGL.so.1` / `libGLESv2.so.2` (found by loading the real FaceLandmarker in the image). Python deps added now: `mediapipe` 1.1.0, `opencv-python-headless`, `av`; the rest of §3's list is added when first used. Container user has a real home dir (matplotlib/fontconfig caches). uvicorn runs with `uvicorn_logging.json` (JSON lines, own access log disabled in favour of the app's).
- Reason: plan leaves the model URL/hash open; the extra libraries are needed for MediaPipe to work.
- Approved by: plan leaves open (library additions are additive; flag if you object)

## 2026-10-12 — Admin session and password details (T6)
- Plan section: §6, §10
- Decision: admin session is a stateless signed cookie (itsdangerous, `SECRET_KEY`, 12 h `ADMIN_SESSION_MAX_AGE_S`), HttpOnly, SameSite=Strict, Secure unless `ADMIN_COOKIE_SECURE=false` (plain-http local dev only). It holds the admin id and a CSRF token; logout clears the cookie but cannot revoke a copied one before it expires. CSRF: hidden `csrf_token` in forms (incl. login, via an anonymous session), `X-CSRF-Token` header on JSON routes (`csrf_protected` dependency); token rotates on login. Minimum password length 12 (`ADMIN_MIN_PASSWORD_LENGTH`). `create-admin` reads the password via hidden prompt or `--password-stdin` (for `setup.sh`). Unknown usernames are hashed against a dummy to avoid a timing side channel. No lockout or rate limit in 1a.
- Reason: plan specifies cookie login, argon2 and CSRF but not lifetime, storage, length or CLI input.
- Approved by: lifetime/lockout/role approved 2026-10-12 (Nágó Dániel); stateless cookie and min length plan leaves open

## 2026-10-12 — DB schema choices left open by §4
- Plan section: §4
- Decision: enums stored as VARCHAR(32) + named CHECK constraint (not native PG enums) so migrations stay simple. Job status values: `queued`, `running`, `done`, `failed` (plan lists none). Job gets `created_at`/`started_at`/`finished_at`. Session has no unique (study_id, participant_code): withdrawal deletes "all data for that participant code", implying several sessions per code (e.g. a redo). Session token expiry (24 h) is derived from `created_at`, no extra column. Config/aois/client_info/quality/flags are JSONB. Primary keys: ints for Study/Video/Station/Job/AdminUser, UUID for Session.
- Reason: plan leaves these open.
- Approved by: plan leaves open

## 2026-10-12 — Pipeline B model weights (yakhyo/gaze-estimation ONNX)
- Plan section: §8.5, §11.2
- Decision: use yakhyo/gaze-estimation ONNX weights (L2CS-style, Gaze360-trained), release `weights`, ONNX opset 20, exported from PyTorch 2.8.0.
  - Primary: `resnet50_gaze.onnx`, https://github.com/yakhyo/gaze-estimation/releases/download/weights/resnet50_gaze.onnx, 95,425,874 bytes, sha256 `bb28d421565adc4dfb665742f8fc80bdef36dd8caa0c87e040e0937f9fdca9a6`, Gaze360 MAE 11.34°.
  - Optional CPU fallback: `mobileone_s0_gaze.onnx`, .../mobileone_s0_gaze.onnx, 4,974,521 bytes, sha256 `8b4fdc4e3da44733c9a82e7776b411e4a39f94e8e285aee0fc85a548a55f7d9f`, MAE 12.58°.
  - Not used: `resnet18_gaze.onnx` (45,066,134 bytes, sha256 `404fec1efd07ff49f981e47f461c20c2627119e465ec441bbd1c067d3f16e657`).
  - I/O: input `input` float32 [1,3,448,448], RGB, pixels scaled to 0–1 then ImageNet mean [0.485,0.456,0.406] / std [0.229,0.224,0.225]. Outputs yaw and pitch, each [1,90] logits over bins. Decode: softmax, expected = Σ(p·i) for i=0..89, angle_deg = expected*4 − 180, convert to radians.
  - Crop rule (fixed, must not change between runs): landmark bounding box expanded 20 %, resized to 448×448 (PLAN §8.5). The reference code uses a face-detector crop, so outputs differ slightly; per-session calibration absorbs this.
  - Licence: code MIT; weights trained on Gaze360, non-commercial research use only. Acceptable for this project; note in README (T13).
- Reason: plan leaves weight source open; user supplied URLs and hashes.
- Approved by: Nágó Dániel

## 2026-10-12 — Milestone 1a scope choices
- Plan section: §11.1, §11.4, §3, §14
- Decision: (1) Browser MediaPipe WASM + model are bundled into `dist/` in 1a (T8), not deferred to M2. (2) GPU compose profile / `worker-gpu` deferred to M5. (3) `docs/DATA_FORMATS.md` deferred until the first files are written (M2/M3). (4) Admin JSON API under `/admin/api/*`; everything not `/api`, `/admin`, `/media` served as static SPA with fallback. (5) Admin cookie 12 h, single role, no lockout in 1a. (6) vitest for frontend unit tests. (7) `AdminUser` and worker heartbeat tables added beyond §4. (8) CI runs a health+login smoke test in 1a; Playwright E2E added in M2. (9) `face_landmarker.task` taken from the official Google storage URL; pinned URL and sha256 recorded here when T7 is implemented.
- Reason: plan leaves these open or sequences them later; confirmed in `/milestone-start 1a` review.
- Approved by: Nágó Dániel

## 2026-10-12 — Milestone 1 split into 1a and 1b
- Plan section: §14
- Decision: Milestone 1 is split into 1a (infrastructure) and 1b (skeleton app), each with its own acceptance check.
- Reason: milestone 1 was too large for one planning cycle.
- Approved by: Nágó Dániel (also known as Champ)
