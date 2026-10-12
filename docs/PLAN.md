# Webcam Eye-Tracking for Educational Video Studies — Implementation Plan

This document is the implementation brief for building a system that records where students look while watching educational videos, using only a standard webcam. Read it fully before starting. Decisions marked **(fixed)** were made by the project owner and should not be changed without asking. Everything else is a recommended default that may be adjusted if there is a concrete technical reason; document any deviation in `docs/DECISIONS.md`.

---

## 1. Goal and scope

**Goal.** Students watch one or more educational videos in a browser. During playback the system records their webcam, the exact playback position of the video, and calibration data. Recordings are uploaded to a Python backend, which estimates gaze offline, maps it onto the video timeline, and produces per-session data and cross-participant analyses (dynamic heatmaps, area-of-interest metrics, attentional synchrony).

**In scope**
- Participant web app: consent, device and quality checks, calibration, validation, video playback, webcam recording, upload.
- Python backend: API, storage, offline processing pipeline, analysis outputs, minimal researcher admin interface.
- Two gaze pipelines, so their accuracy can be compared on the same recordings:
  - **Pipeline A:** MediaPipe Face Landmarker features plus per-participant regression.
  - **Pipeline B:** a deep appearance-based gaze model (L2CS-Net, ONNX) plus per-participant regression.

**Out of scope**
- Real-time gaze display to participants.
- Mobile phones and tablets.
- Gaze outside the browser (other applications or tabs).
- Hardware eye trackers.

**Fixed decisions**
- **(fixed)** Raw webcam video **and** browser-side face features are uploaded and stored on the server.
- **(fixed)** Stimulus videos are self-hosted MP4 files served by the system (no YouTube).
- **(fixed)** Sessions run both in a supervised lab and remotely on students' own laptops. Design for remote first; lab mode adds options.
- **(fixed)** Frontend is TypeScript + Vite, no UI framework.
- **(fixed)** Backend is Python.
- **(fixed)** The browser checks calibration quality immediately and lets the participant redo it (section 5.6).

**Engineering principles**
- **KISS:** prefer the simplest solution that meets the requirement; add complexity only when a test or real data shows it is needed.
- **YAGNI:** do not build features or abstractions that this plan does not ask for.
- **Single source of truth:** shared definitions (landmark subset, event types, config schema) live in one place and are generated or mirrored, never retyped by hand in two places.
- **Raw data is immutable:** processing only ever reads `raw/` and writes `derived/`; everything derived can be deleted and regenerated.
- **Fail loudly:** never silently drop data or substitute defaults; log a clear error, flag the session, and move on.
- **Small, pure, typed units:** pipeline stages are pure functions of their inputs; `mypy --strict` and `tsc --strict` pass.
- **Configuration over constants:** every threshold mentioned in this plan is a named config value with the stated default, not a literal in code.
- **Measure before optimizing:** optimize only what profiling shows to be slow.

---

## 2. Architecture overview

```
┌───────────────────────── Participant browser ─────────────────────────┐
│  Session flow (consent → checks → calibration → video → validation)   │
│  ├─ Stimulus player  (<video>, requestVideoFrameCallback logging)     │
│  ├─ Webcam recorder  (MediaRecorder, 2 s chunks)                      │
│  ├─ Live face model  (MediaPipe Face Landmarker, ~15 fps)             │
│  │     → quality feedback + compact feature stream                    │
│  ├─ Event log        (all events on one performance.now() clock)      │
│  └─ Upload queue     (chunked, resumable, IndexedDB-backed)           │
└───────────────────────────────┬───────────────────────────────────────┘
                                │ HTTPS
┌───────────────────────────────▼───────────────────────────────────────┐
│ Caddy: TLS, static frontend, stimulus MP4s (range requests), proxy    │
│ api (FastAPI): sessions, chunk ingest, admin endpoints                │
│ worker (Python): processing jobs from a DB-backed queue               │
│ PostgreSQL: studies, sessions, jobs, metadata                         │
│ Filesystem: raw recordings, derived data, analysis outputs            │
└───────────────────────────────────────────────────────────────────────┘
```

Deployment: one Linux server, fully dockerized with `docker compose` (services `proxy`, `migrate`, `api`, `worker`, `db`; see section 11). A GPU is optional. Pipeline B runs on CPU overnight for a class-sized study; use the ONNX Runtime CUDA provider if a GPU is available.

---

## 3. Repository layout

```
eyetrack/
  frontend/                 # TypeScript + Vite
    src/
      main.ts
      config.ts             # fetched study config types
      clock.ts              # shared timebase
      api.ts                # backend client
      eventLog.ts
      upload/
        uploadQueue.ts      # chunk queue, retries, IndexedDB persistence
      media/
        camera.ts           # getUserMedia, constraints, track settings
        recorder.ts         # MediaRecorder wrapper
        stimulusPlayer.ts   # video element, preload, frame logging
      face/
        faceWorker.ts       # MediaPipe Face Landmarker (Web Worker if feasible)
        features.ts         # compact feature extraction
        quality.ts          # live quality checks
        quickCalib.ts       # in-browser calibration quality gate (5.6)
        ridge.ts            # small closed-form ridge regression
      screens/              # one module per screen in the session flow
        consent.ts, deviceCheck.ts, instructions.ts, calibration.ts,
        validation.ts, playback.ts, upload.ts, done.ts, error.ts
      ui/                   # small DOM helpers and CSS
  backend/
    app/
      main.py               # FastAPI app
      settings.py           # pydantic-settings
      db.py, models.py      # SQLAlchemy 2.x
      schemas.py            # pydantic models (API + file formats)
      api/
        participant.py      # session + chunk endpoints
        admin.py            # researcher endpoints
      admin_ui/             # Jinja2 templates for minimal admin pages
      storage.py            # path helpers, atomic writes
      jobs.py               # DB-backed job queue
    worker/
      run.py                # job loop
      pipeline/
        ingest.py           # chunk assembly, remux, probe
        sync.py             # timebase alignment
        landmarks.py        # MediaPipe (Python) on full-res frames
        features_a.py       # Pipeline A features
        model_b.py          # L2CS-Net ONNX inference
        calibration.py      # regression fitting, CV
        predict.py          # gaze prediction + validity masks
        smoothing.py        # offline bidirectional smoothing
        validation.py       # accuracy, precision, drift
        fixations.py        # I-DT
        stimulus_map.py     # gaze → video timeline and coordinates
        quality.py          # session quality report
      analysis/
        heatmap_video.py    # dynamic heatmap overlay MP4
        heatmap_static.py
        aoi.py              # AOI metrics
        synchrony.py        # attentional synchrony
        compare.py          # Pipeline A vs B report
    cli.py                  # Typer CLI: process, analyze, delete, export, render-debug
  notebooks/
    inspect_session.ipynb   # ad-hoc session inspection (section 12.3)
  shared/
    landmark_subset.json    # single source for TS and Python feature code
    tests/
  docker-compose.yml
  compose.dev.yml
  .env.example
  Caddyfile
  Makefile
  scripts/                  # setup.sh, fetch_models.sh, backup.sh, restore.sh
  models/                   # L2CS weights (not committed)
  docs/
    DECISIONS.md
    DATA_FORMATS.md         # generated from section 7 and kept up to date
    RUNNING_A_STUDY.md      # researcher guide
```

Python 3.11+. Key dependencies: `fastapi`, `uvicorn`, `sqlalchemy`, `psycopg`, `pydantic-settings`, `jinja2`, `typer`, `mediapipe`, `opencv-python-headless`, `av` (PyAV), `numpy`, `scipy`, `pandas`, `pyarrow`, `scikit-learn`, `onnxruntime`, `matplotlib`. System: `ffmpeg`.

Frontend: `@mediapipe/tasks-vision`, plus `idb` (or hand-written IndexedDB wrapper). No other runtime dependencies are needed.

---

## 4. Domain model

**Study**: id, name, code (short string participants enter or that is embedded in the link), mode (`remote` | `lab`), config (JSON, see 4.1), created_at.

**Video**: id, title, file path, duration, width, height, fps, sha256, `aois` (JSON, optional, see 9.3).

**Station** (lab mode only): id, name, screen_width_cm, screen_height_cm, viewing_distance_cm. These are used to report errors in degrees.

**Session**: id (UUID), study_id, participant_code (pseudonymous; **never** real names), station_id (nullable), upload_token (random, hashed in DB), status (`created` → `consented` → `recording` → `uploaded` → `processing` → `processed` | `failed` | `withdrawn`), consent_version, consent_at, client_info (JSON), quality summary (JSON), raw_video_deleted_at, is_debug (bool), flags (JSON list, e.g. `calibration_gate_failed`, `interrupted`), created_at.

**Chunk**: session_id, stream (`webcam` | `features` | `events` | `stimulus_frames`), seq, size, sha256, received_at. Primary key is (session_id, stream, seq).

**Job**: id, kind (`process_session` | `analyze_video` | `compare_pipelines`), args (JSON), status, attempts, log, timestamps. The worker claims jobs with `SELECT … FOR UPDATE SKIP LOCKED`.

### 4.1 Study config (JSON, editable in admin)

```json
{
  "playlist": ["<video_id>", "..."],
  "calibration": { "points": 13, "dot_duration_ms": 1500, "margin": 0.06 },
  "validation": { "points": 5, "before_video": true, "between_videos": true, "after_last_video": true },
  "calibration_gate": { "enabled": true, "max_mean_error_norm": 0.10, "max_point_error_norm": 0.18,
                        "min_valid_frames_per_dot": 5, "max_attempts": 3, "on_fail": "continue_flagged" },
  "playback": { "allow_pause": false, "require_fullscreen": true },
  "recording": { "width": 1280, "height": 720, "fps": 30, "chunk_ms": 2000 },
  "quality": { "min_face_ratio_warn": 0.8, "max_face_ratio_warn": 1.25 },
  "inclusion": { "max_validation_error_norm": 0.12, "min_valid_fraction": 0.7 },
  "retention": { "raw_video_days_after_processing": null },
  "consent_version": "v1",
  "debug_allowed": false
}
```

`retention.raw_video_days_after_processing = null` means raw video is kept until manually deleted. The researcher sets this per study according to the ethics approval.

---

## 5. Participant web app

### 5.1 Supported environments
- Desktop Chrome and Edge (current versions) are the primary targets. Firefox is best effort. Show a clear message on unsupported browsers (Safari, mobile).
- Detect the needed APIs and block the session with a clear message if any are missing: `getUserMedia`, `MediaRecorder` with a supported WebM codec, `requestVideoFrameCallback`, `requestFullscreen`, IndexedDB, WebAssembly.

### 5.2 Session flow

1. **Entry.** The URL contains the study code (`/s/<study_code>`). The participant enters their participant code (or it is prefilled via `?p=`). `POST /api/sessions` returns the session id, upload token and study config.
2. **Consent.** Show the consent text for the study's `consent_version`, including that webcam video is recorded and stored. Recording only starts after explicit agreement. `POST /api/sessions/{id}/consent`.
3. **Video preload.** Download every playlist MP4 completely in the background (`fetch` → Blob → object URL), with a progress bar. Playback must never stall on network buffering. Verify sha256 against the config.
4. **Device check.**
   - Request the camera (1280×720 ideal, 30 fps ideal; accept ≥ 640×480). Record the actual `track.getSettings()`.
   - Start the live face model and show the camera preview with an overlay.
   - Checks: face detected continuously for 3 s; brightness of the face region within bounds; face size (interocular distance as a fraction of frame width) within a target band that corresponds to roughly arm's length; only one face; head roughly frontal.
   - Show specific, actionable messages ("Move closer", "Your face is too dark — turn on a light in front of you, avoid windows behind you", "Only one person should be in view").
   - Lab mode: the researcher selects the station on this screen.
5. **Instructions.** Keep your head still, sit at the current distance, do not cover your face, avoid leaving fullscreen.
6. **Enter fullscreen.** Required if `require_fullscreen`. From this point, `MediaRecorder` starts, and recording continues until the end-of-session validation is complete.
7. **Calibration** (section 5.5).
8. **Validation** (pre-video) with the **calibration quality gate** (section 5.6). If the gate fails, repeat steps 7–8.
9. **Playback** of each playlist video, with between-video validation if configured.
10. **Final validation.**
11. **Stop recording, finish upload.** Show progress and do not allow the participant to close the page until everything is uploaded (`beforeunload` warning). On success, `POST /api/sessions/{id}/complete`.
12. **Done.**

If fullscreen is exited during playback: pause the video, log the event, show "Return to fullscreen to continue", and resume on re-entry. The same applies when face tracking reports no face for more than 3 s during playback: log the event and show a non-blocking banner. **Do not pause in this case**, since the dropout itself is data.

### 5.3 Timebase (critical)

All client-side timestamps use `performance.now()` (ms, float), called `t` everywhere. At session start, log `performance.timeOrigin` and `Date.now()` once for wall-clock mapping. Never use `Date.now()` for intervals.

Each of the following must be logged with `t`:
- **Stimulus frames.** Use `video.requestVideoFrameCallback` on the stimulus video for every presented frame: `{t: now, expectedDisplayTime, mediaTime, presentedFrames}`. This is the authoritative mapping from the client clock to video position. Write it to the `stimulus_frames` stream.
- **Webcam frames as seen live.** Use `requestVideoFrameCallback` on the hidden webcam preview `<video>`, logged with `{t, captureTime?, mediaTime, presentedFrames}`, at every frame where the face model ran.
- **Recorder lifecycle.** `recorder_start` (in the `start` event handler), each `dataavailable` (with chunk seq), `recorder_stop`.

### 5.4 Webcam recording and feature stream

- Use `MediaRecorder` with mime type preference order `video/webm;codecs=vp9`, `video/webm;codecs=vp8`, `video/webm`, and bitrate ≈ 2.5 Mbps. Use `timeslice = chunk_ms` (2000 ms).
- Each `dataavailable` blob becomes a chunk `{stream: "webcam", seq}`. Chunks of one recording are a single continuous WebM byte stream; only chunk 0 contains the header. The server reassembles by byte concatenation.
- **Live face model.** Run MediaPipe Face Landmarker (tasks-vision, VIDEO running mode, `outputFaceBlendshapes: true`, `outputFacialTransformationMatrixes: true`) at about 15 fps. Use a Web Worker with an `OffscreenCanvas`/`ImageBitmap` transfer if it works reliably; otherwise run on the main thread, throttled. Playback smoothness has priority: if the stimulus video drops frames (check `presentedFrames` gaps), reduce face model fps.
- **Compact feature record per processed frame** (JSON lines, gzip per chunk, stream `features`, chunked every 2 s):
  ```json
  {"t": 12345.6, "webcam_media_time": 41.233, "face": true,
   "lm": [[x,y,z], ...],            // fixed subset, see below
   "matrix": [16 floats],          // facial transformation matrix
   "blink_l": 0.03, "blink_r": 0.04,
   "face_ratio": 0.162, "brightness": 0.47}
  ```
  Landmark subset: both iris rings (468–477), eye contours (left/right upper and lower lids, corners), nose tip, chin, forehead, cheeks. Define the exact index list once in `features.ts` and mirror it in `backend/worker/pipeline/features_a.py`, with a shared JSON file `shared/landmark_subset.json` used by both.
- The feature stream has two purposes: (1) a fallback and cross-check for Pipeline A if the raw video is unusable, and (2) fine sync alignment (section 8.2).

### 5.5 Calibration and validation screens

- Background: neutral mid-grey. Stimulus area: full viewport (fullscreen).
- **Calibration** uses 13 points: a 3×3 grid plus 4 points at the centres of the four quadrants, with the outer points inset by `margin` (6 % of the viewport). Randomise the order, but always start with the centre point.
- **Per dot:**
  - A dot appears (outer radius 20 px, dark centre 4 px) and shrinks to 6 px over `dot_duration_ms`.
  - The participant clicks the dot once it has shrunk (the click is accepted only in the last 600 ms; an early click flashes the dot, indicating "wait").
  - Log `calib_dot_show {id, x, y}`, `calib_dot_click {id}`, `calib_dot_hide {id}`.
  - The training window for the dot is from 700 ms before the click to 100 ms after it. This excludes the saccade at onset.
- **Validation** uses 5 points (centre, 4 inner corners at 25 %/75 %), with the same dot behaviour. These points are **never** used for fitting; they are only used for measuring error.
- Log the viewport size, `screen.width/height`, `devicePixelRatio`, and the stimulus video's `getBoundingClientRect()` whenever any of them changes (`resize` event).

### 5.6 Calibration quality gate (in the browser)

**Purpose:** detect a bad calibration while the participant is still there and let them redo it, instead of discovering an unusable session after upload. The browser result is only a pass/fail gate; the server's fit (section 8.6) remains the authoritative result and is computed independently.

**Procedure** (runs right after the pre-video validation block):
1. **Collect samples.** From the live feature stream (5.4), take the frames inside each calibration dot's training window (same window as 5.5) that have a face and no blink.
2. **Compute features.** Use the Pipeline A feature definition (section 8.4): iris position in eye-local coordinates for both eyes, eye openness, head yaw/pitch/roll and face scale. Implement it once in TypeScript in `features.ts`, following `shared/landmark_subset.json`. Add a test that runs the TS and Python implementations on the same recorded fixture frames and asserts they agree within 1e-4.
3. **Fit.** Standardize features, apply a degree-2 polynomial expansion, and fit closed-form ridge regression (`ridge.ts`, α = 1.0 fixed, separate x and y outputs). With 13 dots × ~20 frames this is a tiny linear system solved in milliseconds; no ML library is needed.
4. **Measure.** Predict on the validation block's frames. For each validation point, take the median prediction and compute its distance to the target, normalized by viewport diagonal (`error_norm`).
5. **Decide.** Pass if *all* hold:
   - mean `error_norm` ≤ `calibration_gate.max_mean_error_norm` (default 0.10);
   - no single point above `max_point_error_norm` (default 0.18);
   - every calibration dot had ≥ `min_valid_frames_per_dot` usable frames.

**On failure:**
- Show the participant a short, non-technical message with the most likely cause, chosen from the quality signals during the calibration: too few face frames ("Keep your face in view"), low brightness ("Add light in front of you"), large face-size change or head movement ("Keep your head still and stay at the same distance"), otherwise "Let's try once more — follow each dot closely with your eyes."
- Repeat calibration and the pre-video validation. Each attempt is a new, separately logged calibration; the server uses only the **last passed** attempt (or the last attempt if none passed).
- After `max_attempts` (default 3) failures, behaviour follows `on_fail`: `continue_flagged` (default; the session continues and is flagged `calibration_gate_failed`, since the server fit may still succeed) or `end_session` (thank the participant and end).

**Logging:** event `calib_gate_result {attempt, passed, mean_error_norm, point_errors[], valid_frames_per_dot[], reason}`. Show the attempt count and errors in the admin quality report.

**Later validation blocks** (between and after videos) are also scored in the browser and logged with the same event type, but they never block or interrupt the participant. In debug mode (section 12) the gate's numbers are shown on screen.

**Never** show gaze predictions or numeric errors to participants outside debug mode.

### 5.7 Playback screen

- Stimulus video fills the viewport while keeping its aspect ratio (letterboxed on black). Log the content rectangle in viewport CSS px. Compute it from the video's intrinsic size and `object-fit: contain`; do not rely on the element's box.
- No native controls. If `allow_pause` is true, the space bar toggles pause. Seeking is not allowed.
- Log `video_load`, `video_play`, `video_pause`, `video_ended`, `video_stall` (from `waiting`), `visibility_change`, `fullscreen_change`, `window_blur/focus`, and `face_lost`/`face_regained` (debounced 500 ms).

### 5.8 Upload queue

- Every chunk (all four streams) is first written to IndexedDB, then uploaded with `PUT /api/sessions/{id}/chunks/{stream}/{seq}` (body: raw bytes; header `X-Chunk-SHA256`; bearer upload token).
- Uploads are sequential per stream, run in the background during the session, and retry with exponential backoff (max 30 s delay). Delete a chunk from IndexedDB only after a 2xx response.
- On page reload with an unfinished session in IndexedDB, offer "Resume upload" (not resume recording; a reloaded session is ended and flagged as `interrupted`).
- `POST /complete` sends `{stream: last_seq}` for every stream. The server responds with any missing seqs, and the client re-sends them.

### 5.9 Events stream

The `events` stream holds JSON lines `{t, type, data}` for everything in 5.2–5.8, uploaded in chunks the same way. Types are an enum defined in `eventLog.ts` and mirrored in `backend/app/schemas.py`. Include `session_start` with `client_info`: user agent, platform, `hardwareConcurrency`, camera settings, chosen mime type, screen info.

---

## 6. Backend API

All participant endpoints use the upload token issued at session creation. Admin endpoints use a session cookie from a simple username/password login (argon2 hashes, accounts created via CLI). The system runs under HTTPS only; `getUserMedia` requires a secure context anyway.

**Participant**

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/studies/{code}` | Public study info (name, consent text, browser requirements) |
| POST | `/api/sessions` | `{study_code, participant_code}` → `{session_id, upload_token, config, videos:[{id,url,sha256,duration}]}` |
| POST | `/api/sessions/{id}/consent` | `{consent_version}` |
| PUT | `/api/sessions/{id}/chunks/{stream}/{seq}` | Raw bytes; idempotent (same sha256 → 200, different → 409) |
| POST | `/api/sessions/{id}/complete` | `{last_seq: {stream: n}}` → `{missing: {stream: [seq]}}`; when nothing is missing, sets status `uploaded` and enqueues `process_session` |
| GET | `/media/videos/{id}.mp4` | Served by the proxy (Caddy) directly with range support |

**Admin** (JSON API + Jinja pages)
- Studies: create, edit config, view participant link.
- Videos: upload MP4 (server validates with ffprobe: H.264, constant frame rate preferred; warn otherwise), edit AOIs JSON, view duration and fps.
- Stations: CRUD.
- Sessions: list with status and quality summary, view the quality report, re-run processing, delete raw video, withdraw participant (deletes **all** data for that participant code in that study, raw and derived, and logs the deletion without personal data).
- Analysis: trigger `analyze_video` for a video across included sessions; trigger `compare_pipelines`; download outputs (ZIP).
- Jobs: list with logs.

**Limits:** chunk max 10 MB; per-session total configurable (default 2 GB). Rate-limit session creation per IP.

---

## 7. Storage layout and file formats

```
DATA_ROOT/
  videos/{video_id}/video.mp4
  videos/{video_id}/aois.json
  studies/{study_id}/sessions/{session_id}/
    raw/
      chunks/{stream}/{seq:06d}.bin
      webcam.webm             # assembled, remuxed
      events.jsonl
      stimulus_frames.jsonl
      browser_features.jsonl
      meta.json               # copy of session row + client_info + config snapshot
    derived/
      sync.json
      landmarks.parquet       # server-side MediaPipe, full-res
      features_a.parquet
      features_b.parquet      # L2CS outputs
      calibration_{a,b}.json  # fitted model params + CV error
      gaze_{a,b}.parquet
      fixations_{a,b}.parquet
      quality.json
  analysis/{study_id}/{video_id}/{run_id}/...
```

Write all files atomically: write to `*.tmp`, then rename. Use Parquet with snappy compression. Document every schema in `docs/DATA_FORMATS.md`.

**`gaze_{a,b}.parquet`** (one row per webcam frame):

| column | type | meaning |
|---|---|---|
| `t` | float64 | client clock ms |
| `webcam_pts` | float64 | seconds in webcam.webm |
| `video_id` | string, nullable | stimulus playing at this time |
| `stim_time` | float64, nullable | stimulus media time in seconds |
| `x_px`, `y_px` | float32 | viewport CSS px, raw prediction |
| `x_sm`, `y_sm` | float32 | after smoothing |
| `x_vid`, `y_vid` | float32, nullable | normalized to video content rect, 0–1 (can fall outside) |
| `valid` | bool | face found, not blinking, within plausible range |
| `blink` | bool | |
| `conf` | float32 | 0–1 heuristic |
| `phase` | string | `calibration`, `validation`, `playback`, `other` |

---

## 8. Processing pipeline (`process_session` job)

Each stage is a pure function from input files to output files, idempotent, and individually re-runnable via the CLI (`cli.py process <session_id> --from-stage sync`). Each stage records its duration and a summary in `quality.json`.

### 8.1 Ingest
- Verify all chunks are present and checksums match.
- Concatenate webcam chunks, then remux with `ffmpeg -i in.webm -c copy -fflags +genpts out.webm`. MediaRecorder WebM files lack cues and duration; remuxing makes them seekable. Also produce per-frame pts via `ffprobe -show_frames` or PyAV. The frame rate is variable; never assume constant fps.
- Concatenate and decompress the JSONL streams.

### 8.2 Sync — map webcam frame pts to client clock `t`
1. **Coarse offset:** `t ≈ t_recorder_start + pts*1000`.
2. **Fine alignment:** run server-side MediaPipe (8.3) first, then cross-correlate a per-frame signal available in both the browser features and the server landmarks (horizontal iris position minus eye-corner midpoint, plus head yaw from the transformation matrix, z-scored), resampled to 100 Hz. Search a lag of ±1000 ms around the coarse offset.
3. **Drift:** repeat the correlation in 60 s windows and fit `t = a + b*pts` with a robust linear fit (Huber). If |b − 1| > 0.001, or the residuals exceed 40 ms, flag the session.
4. Write `sync.json` with the offset, slope, correlation peak values, and flags.
5. Map client `t` to stimulus time using `stimulus_frames.jsonl`. For a given `t`, find the stimulus frame whose `expectedDisplayTime` precedes it, and interpolate `mediaTime`. Gaps (pause, stall, between videos) give `stim_time = null`.

### 8.3 Landmarks (server side)
MediaPipe Face Landmarker (Python, VIDEO mode) on every decoded webcam frame at full resolution, with blendshapes and transformation matrix enabled. Store the full 478 landmarks as float32 in `landmarks.parquet`.

### 8.4 Pipeline A features
Per frame, for each eye:
- Iris centre (mean of iris ring landmarks) expressed in an eye-local frame: origin at the midpoint of the eye corners, x-axis along the corner-to-corner vector, scaled by eye width. Use both coordinates.
- Eye openness (lid distance / eye width).

Plus head pose: yaw, pitch, roll and translation from the facial transformation matrix, and face scale (interocular distance).

`blink = eyeBlink blendshape > 0.5 for either eye OR openness < threshold` (tune on calibration data).

### 8.5 Pipeline B features
- L2CS-Net (MobileOne or ResNet-50 variant), exported to ONNX. Input: face crop from the landmarks' bounding box, expanded 20 %, resized to 448×448 and normalized as in the original repository. Output: pitch and yaw (radians).
- Features: gaze pitch/yaw + the head pose features from 8.4.
- Place the model weights under `models/` (not committed). Record their source, version and sha256 in `docs/DECISIONS.md`.
- **License note:** L2CS-Net weights trained on Gaze360 are restricted to non-commercial research use. This is acceptable for this academic project. Record it in the README.

### 8.6 Calibration fit (per pipeline, per session)
- Training samples: frames within each calibration dot's training window (5.5) that are `valid` and not blinking, from the **last calibration attempt that passed the browser gate** (5.6), or the last attempt if none passed. Target: dot position (viewport px).
- **Model:** standardize features, then degree-2 polynomial expansion, then ridge regression, with separate outputs for x and y. Choose the regularization α by leave-one-dot-out cross-validation over a log grid (1e-3 … 1e3).
- Also fit a plain linear ridge model and keep the one with the lower leave-one-dot-out error. Record both errors.
- Reject outlier samples per dot (beyond 2.5 MAD of the dot's median prediction in a first pass), then refit.
- If more than 3 calibration dots have fewer than 5 valid frames, mark calibration as `failed`.
- Save the parameters, the chosen model, and the CV error to `calibration_{a,b}.json`.

### 8.7 Prediction, validity, smoothing
- Predict `x_px`, `y_px` for all frames.
- `valid = face ∧ ¬blink ∧ prediction within the viewport expanded by 15 %`.
- **Offline smoothing:** linearly interpolate across invalid gaps ≤ 150 ms (mark interpolated samples, but leave `valid` false); then apply a Rauch–Tung–Striebel (forward-backward) Kalman smoother with a constant-velocity model. Process noise must allow saccades: tune so that a 300 px step settles within ≈ 60 ms on synthetic data. Gaps longer than 150 ms break the smoother into segments.

### 8.8 Validation and drift
- For each validation block, compute the error per point from frames in that point's training window:
  - **accuracy:** distance between the median prediction and the target;
  - **precision:** RMS of sample-to-sample distance.
- Report errors in px, normalized by viewport diagonal (`error_norm`), and in degrees of visual angle when a station with physical screen size and distance is known (lab mode).
- **Drift:** the mean offset vector at each validation block. If several blocks exist, provide `drift_corrected` gaze columns using time-linear interpolation of the offset between blocks. Keep the uncorrected columns too, and report both in `quality.json`.

### 8.9 Fixations
Use I-DT on smoothed, valid samples during playback:
- dispersion threshold = max(0.05 × viewport width, 2 × session validation precision);
- minimum duration 100 ms.

Output per fixation: start/end `t`, `stim_time` start/end, centroid in px and video-normalized coordinates, duration. Make thresholds configurable and record them in the output metadata.

### 8.10 Quality report (`quality.json`)
Valid fraction during playback; webcam effective fps; face-lost episodes; fullscreen exits; stalls; validation accuracy/precision per block; drift; sync flags; calibration CV error; inclusion decision against `study.config.inclusion` with reasons. Show this report in the admin UI.

---

## 9. Analysis (`analyze_video`, `compare_pipelines` jobs)

Inputs: all sessions of a study that watched the video, passed inclusion, and selected pipeline (default A; configurable). Every analysis run gets a `run_id` and writes `run.json` containing parameters, included session ids, excluded ones with reasons, and the code git hash.

### 9.1 Dynamic heatmap video
- For each stimulus frame time `s`, collect valid gaze samples (video-normalized coordinates) from all included sessions with `stim_time ∈ [s − 0.5, s + 0.5]`.
- **Density:** sum of Gaussians, with σ equal to the median validation accuracy of the included sessions in video-normalized units, but at least 0.03. Weight each participant equally (normalize per participant before summing).
- Render: colormap with alpha, overlay on the decoded stimulus frame, write H.264 MP4 at the stimulus fps with ffmpeg via PyAV. Optionally also draw per-participant dots.
- Also export the per-frame density grid at 64×36 resolution as `.npz`.

### 9.2 Static heatmaps
For each researcher-defined time segment (from `aois.json` `segments`, or fixed 10 s windows by default), produce a PNG over a representative frame, using the same density method as 9.1.

### 9.3 AOI metrics
`videos/{id}/aois.json`:

```json
{
  "segments": [{"id": "intro", "start": 0.0, "end": 32.5}],
  "aois": [
    {"id": "code_panel", "label": "Code", "start": 12.0, "end": 95.0,
     "shape": "rect", "coords": [0.55, 0.10, 0.98, 0.90]},
    {"id": "dancers", "label": "Dancers", "start": 0.0, "end": 180.0,
     "shape": "polygon", "coords": [[0.1,0.2],[0.5,0.2],[0.5,0.9],[0.1,0.9]]}
  ]
}
```

Coordinates are video-normalized. Per participant × AOI, compute:
- dwell time (fixation-based and sample-based);
- proportion of the AOI's active time spent looking at it;
- time to first fixation;
- fixation count;
- revisits.

Export a long-format CSV (`participant_code, aoi_id, metric, value`) plus a wide CSV. Include an AOI hit tolerance option (expand shapes by k × validation accuracy, default k = 0) and report it in `run.json`.

### 9.4 Attentional synchrony
For each 200 ms window, compute the dispersion of participant gaze positions: the mean distance of each participant's median gaze to the group median, in video-normalized units. Also compute the normalized scanpath saliency (NSS) of each participant against the leave-one-out group density. Export a CSV time series and a PNG plot over the video timeline.

### 9.5 Pipeline comparison
For each session, compare the validation accuracy and precision of pipeline A vs B, plus the valid fraction; also compute paired statistics across sessions (Wilcoxon signed-rank), and produce a CSV plus a short Markdown report with box plots.

---

## 10. Privacy, ethics, security

- Participants are identified only by pseudonymous codes. The mapping from code to person is kept by the researcher outside this system.
- The consent screen states: webcam video is recorded and stored on a university server, the purpose, the retention period, and how to withdraw. Store the consent version and timestamp per session. Ethics approval must be obtained before real data collection; the system must support the study's retention rules via `retention.raw_video_days_after_processing` (a daily worker task deletes raw video when due and logs it).
- Withdrawal deletes all of a participant's data in the study (section 6).
- HTTPS only, HSTS. Upload tokens are random 256-bit values, stored hashed, and expire 24 h after session creation.
- `DATA_ROOT` should be on an encrypted volume. Document this requirement in `RUNNING_A_STUDY.md` and do not implement encryption in the app.
- Admin interface: login required, CSRF protection, no participant video served to anyone except logged-in admins.
- Never log raw media or landmark data to application logs.

---

## 11. Deployment (Docker)

**Requirement:** a person who has never seen the code can take a fresh Linux server (Ubuntu 22.04/24.04 with Docker installed), follow `README.md`, and have the system running under HTTPS in under 15 minutes, with no manual edits beyond one `.env` file. Everything runs in containers; nothing is installed on the host except Docker Engine and the Compose plugin.

### 11.1 Services (`docker-compose.yml`)

| Service | Image | Role |
|---|---|---|
| `proxy` | `caddy:2` | TLS (automatic Let's Encrypt), serves the built frontend and stimulus MP4s (range requests), reverse-proxies `/api` and `/admin` to `api`. Only service with published ports (80, 443). |
| `migrate` | backend image | One-shot: runs `alembic upgrade head`, then exits. `api` and `worker` depend on it with `condition: service_completed_successfully`. |
| `api` | backend image | `uvicorn app.main:app --workers N` |
| `worker` | backend image | `python -m worker.run`; concurrency via `WORKER_CONCURRENCY`. |
| `db` | `postgres:16` | Not exposed outside the compose network. |

**Note:** Caddy is used instead of nginx because it obtains and renews certificates with no extra container or cron job. If the agent has a strong reason to use nginx, it must also provide certbot automation.

**Images:**
- **Backend image** (one Dockerfile, used by `migrate`, `api`, `worker`): `python:3.11-slim` + `ffmpeg` + `libgl1`/`libglib2.0-0` for OpenCV/MediaPipe. Multi-stage build; dependencies installed with `uv` from a lock file. The MediaPipe `face_landmarker.task` model is downloaded at build time and verified by sha256. Runs as a non-root user.
- **Proxy image:** multi-stage. Stage 1 builds the frontend with `node:20` (`npm ci && npm run build`). Stage 2 is `caddy:2` with the built `dist/` copied in and the `Caddyfile`. MediaPipe WASM and model files for the browser are bundled into `dist/` (self-hosted, no CDN), so the participant app works on networks that block CDNs.

**Every service:** `restart: unless-stopped`, a healthcheck (`api`: `GET /api/health`, which checks DB connectivity; `db`: `pg_isready`; `worker`: heartbeat row updated every 30 s, checked by a small script), and log rotation (`json-file`, `max-size: 20m`, `max-file: 5`).

### 11.2 Volumes and configuration

- `pgdata` named volume for PostgreSQL.
- `DATA_ROOT` is a **bind mount** from a host path set in `.env` (default `./data`), mounted at `/data` in `api`, `worker` and `proxy` (proxy read-only, only `videos/`). A bind mount makes it obvious where recordings live on disk, simplifies backups, and lets the researcher put it on an encrypted volume.
- `models/` is a bind mount for the L2CS-Net ONNX weights, filled by `scripts/fetch_models.sh` (download + sha256 check). The worker must start without these weights and only fail Pipeline B jobs with a clear message.
- **`.env.example`** (committed, documented line by line):
  ```
  DOMAIN=eyetrack.example.org
  ACME_EMAIL=you@example.org
  TLS_MODE=acme              # acme | internal | files
  TLS_CERT_FILE=             # only for TLS_MODE=files
  TLS_KEY_FILE=
  DATA_DIR=./data
  MODELS_DIR=./models
  POSTGRES_PASSWORD=change-me
  SECRET_KEY=change-me       # session cookies, CSRF
  API_WORKERS=2
  WORKER_CONCURRENCY=1
  ONNX_PROVIDER=cpu          # cpu | cuda
  MAX_UPLOAD_CHUNK_MB=10
  ```
- **TLS modes.** `acme` for a server with a public domain and ports 80/443 reachable. `files` for a university server where IT provides a certificate. `internal` (Caddy's local CA) for an internal lab network; `README` explains how to trust the CA on lab machines. HTTPS is mandatory in all modes, since browsers only allow camera access in a secure context.
- The proxy enforces the request body limit (`MAX_UPLOAD_CHUNK_MB`) and sets security headers (HSTS, `Permissions-Policy: camera=(self)`, a CSP that allows WASM).

### 11.3 Setup flow (`README.md`)

```bash
git clone <repo> eyetrack && cd eyetrack
cp .env.example .env && nano .env        # domain, passwords, TLS mode
./scripts/fetch_models.sh                 # optional: Pipeline B weights
docker compose up -d --build
docker compose exec api python cli.py create-admin
```

Provide `scripts/setup.sh` that performs these steps interactively: checks Docker and Compose versions, generates random `POSTGRES_PASSWORD` and `SECRET_KEY` if they are still `change-me`, checks that `DOMAIN` resolves to this server when `TLS_MODE=acme`, starts the stack, waits for healthchecks, and prompts for the admin account.

### 11.4 Operations

- **Update:** `git pull && docker compose up -d --build`. Migrations run automatically via `migrate`.
- **Backup:** `scripts/backup.sh` writes a `pg_dump` and an `rsync`/`tar` of `DATA_DIR` to a target directory; flag `--no-raw-video` to skip raw webcam files. `scripts/restore.sh` reverses it. Document a restore test in `README.md`.
- **CLI access:** every CLI command runs as `docker compose exec api python cli.py …` (or `worker`); document the common ones (create admin, process session, analyze video, delete raw, withdraw participant, export).
- **Optional GPU:** a compose profile `gpu` with a `worker-gpu` service using `onnxruntime-gpu` and `deploy.resources.reservations.devices` for NVIDIA. Started with `docker compose --profile gpu up -d`. The default CPU setup must work without it.
- **Server sizing (document in README):** 4+ CPU cores, 16 GB RAM, disk ≈ 150 MB per 10-minute session for raw plus derived data (e.g. 50 students × 10 sessions ≈ 75 GB), plus room for analysis videos. Postgres stays small.
- **Firewall:** only 80 and 443 open; Postgres and the API are never published to the host network.

### 11.5 Local development

- `compose.dev.yml` override: Vite dev server with hot reload, `api` with `--reload` and the source mounted, Postgres published on localhost, no TLS (`localhost` counts as a secure context for camera access).
- `make dev`, `make test`, `make e2e`, `make lint` wrap the compose commands.
- CI (GitHub Actions): lint, type check (`mypy`, `tsc`), unit tests, build both images, run the E2E test against the built stack.

### 11.6 Acceptance

Milestone 1a is only done when a fresh VM, using only `README.md` and `scripts/setup.sh`, reaches a healthy HTTPS instance where an admin can log in. Milestone 1b re-runs this check and additionally completes a dummy session.

---

## 12. Debug tooling

Debugging webcam gaze is mostly about *seeing* what happened. Build these tools alongside the features they help with (milestone noted in brackets), not at the end.

### 12.1 Participant app debug mode [M2]
- Enabled with `?debug=1`, but **only** if the study has `debug_allowed: true` in its config or an admin is logged in on the same browser. Sessions run in debug mode are stored with `is_debug = true` and are excluded from analysis by default.
- **Overlay panel** (toggle with `D`):
  - webcam preview with landmarks, iris centres and face bounding box drawn on it;
  - live counters: webcam fps, face model fps and per-frame inference time, stimulus dropped frames, upload queue length and bytes pending, current quality signals (brightness, face ratio, head pose, blink);
  - the last 20 events from the event log.
- **Live gaze dot:** after the calibration gate has run, show the in-browser gaze prediction as a dot (toggle with `G`). This is the quickest way to judge tracking quality by eye.
- **Calibration gate details:** per-point errors drawn as lines from target to median prediction on the validation screen.
- **Shortcuts:** `S` skips the current calibration, validation or video (logged as `debug_skip`); `F` forces the calibration gate to fail, for testing the retry flow.
- **Client errors:** in all modes (not just debug), `window.onerror` and `unhandledrejection` are captured into the events stream and also sent to `POST /api/client-errors` immediately, so failures are visible even if the session never completes.

### 12.2 Session inspector in admin [M3]
For each processed session, the admin page shows:
- **Debug replay video**, rendered by `cli.py render-debug <session_id>` (also triggerable from admin): the stimulus video with gaze dots from both pipelines (raw and smoothed, different colours) and fixation circles, plus the participant's webcam in a corner with landmarks drawn, and a timeline bar of events (pauses, face lost, fullscreen exits). Mark clearly that it contains participant video and only render on request.
- **Plots** (PNG, generated by the pipeline into `derived/debug/`):
  - calibration: targets vs median predictions with error lines, for each attempt and pipeline;
  - validation blocks over time, showing drift;
  - sync: the cross-correlation curve and the drift fit with residuals;
  - quality signals over the session: valid fraction, face ratio, brightness, head pose, webcam fps;
  - gaze x/y over time with invalid segments shaded.
- **Stage table:** each pipeline stage with status, duration, summary values and a "re-run from this stage" button.

### 12.3 Backend and pipeline debugging [M1–M3]
- Structured JSON logs with `session_id`, `job_id` and `stage` on every line; log level set by `LOG_LEVEL` in `.env`. Never log media or landmark arrays.
- `cli.py export-session <id>` writes a ZIP with `raw/`, `derived/` and `meta.json`; `cli.py import-session <zip>` loads it into a local dev instance. This makes any field problem reproducible on a developer machine.
- `cli.py process <id> --from-stage <stage> --only <stage> --keep-intermediates` for re-running single stages.
- A small Jupyter notebook in `notebooks/inspect_session.ipynb` that loads a session's derived files and draws the same plots, for ad-hoc exploration.
- **Admin diagnostics page:** disk usage of `DATA_ROOT`, job queue length and failed jobs, worker heartbeat age, app version (git hash), browser versions and camera settings seen in recent sessions.

### 12.4 Test fixtures for debugging [M2]
- A **moving-dot stimulus video** (dot moving along a known path, with pauses) in the repo, used for the pilot checklist: the processed gaze should follow the known path, and the debug replay makes errors obvious.
- A **recorded face clip** in `.y4m` for the fake-camera E2E test (section 13).

---

## 13. Testing

- **Unit tests (pytest):**
  - calibration fitting on synthetic features with known mappings (must recover within tolerance);
  - RTS smoother step response;
  - I-DT on synthetic scanpaths with known fixations;
  - sync cross-correlation with an injected known offset and drift;
  - stimulus time mapping across pause and stall gaps;
  - AOI hit tests.
- **Backend API tests:** chunk idempotency, missing chunk detection, auth, withdrawal deletes everything.
- **End-to-end:** run Chrome with `--use-fake-device-for-media-stream --use-file-for-fake-video-capture=face.y4m --use-fake-ui-for-media-stream` (Playwright), using a recorded clip of a real face. Drive the full session flow (clicking calibration dots programmatically), then process the session and assert that all derived files exist and the schemas are valid. Gaze accuracy cannot be checked with a fake camera; this test is about plumbing.
- **Researcher pilot checklist** (in `RUNNING_A_STUDY.md`): a researcher looks at known on-screen targets (a test video with a moving dot is included in the repo as a fixture) to check end-to-end accuracy on real hardware before each study.

---

## 14. Milestones

Each milestone ends with a working, tested state and an update to `docs/`. Implementation workflow, progress tracking and review rules are in `CLAUDE.md` at the repository root.

1a. **Infrastructure.** Full Docker setup per section 11 (compose, Caddy TLS, `migrate` service, healthchecks, `.env.example`, `scripts/setup.sh`, `compose.dev.yml`, Makefile, CI), structured logging (12.3), DB models and migrations, admin login and `create-admin` CLI, `/api/health`. *Done when* `scripts/setup.sh` on a fresh VM yields a healthy HTTPS instance where an admin can log in.
1b. **Skeleton app.** Study/video/station/session CRUD in admin, video upload and serving, participant flow screens without tracking (dots can be clicked, video plays), event log, chunked resumable upload of events, `/complete`, client error capture (12.1). *Done when* a full dummy session can be completed and its events appear on the server.
2. **Recording and sync data.** Camera, MediaRecorder upload, stimulus frame logging, browser face model with quality checks and feature stream, fullscreen handling, resume upload, **calibration quality gate (5.6)**, participant debug mode (12.1). *Done when* the E2E test produces complete raw data for a session.
3. **Pipeline A.** Ingest, sync, server landmarks, features A, calibration, prediction, smoothing, validation, fixations, quality report, CLI, session inspector and debug plots (12.2). *Done when* the moving-dot fixture video shows plausible tracking on a real webcam, and the quality report renders in admin.
4. **Analysis.** Dynamic and static heatmaps, AOIs, synchrony, analysis runs in admin, ZIP export.
5. **Pipeline B and comparison.** L2CS ONNX inference, features B, comparison report.
6. **Hardening for pilot.** Retention task, withdrawal, browser-support messages, performance tuning for low-end laptops (playback must not drop frames), `RUNNING_A_STUDY.md`.

---

## 15. Items to confirm with the project owner later (do not block)

- Exact consent text and retention period (depends on the ethics approval).
- AOI definitions for each stimulus video (the researcher authors `aois.json`; an AOI drawing tool is **not** in scope unless requested).
- Inclusion thresholds after the first pilot.
- Whether participants may pause videos (`allow_pause`, default false).
- Whether a short questionnaire should be added after each video (not in scope now).
