# Data formats

What the system writes to disk and sends over the wire, as of Milestone 3 (ingest). Sections are added as later milestones write more files (PLAN §7). The reference for names and payload shapes is `shared/protocol.json`; `backend/app/schemas.py` and `frontend/src/{config,eventLog,records}.ts` mirror it and tests compare all three.

## Files under `DATA_ROOT` (default `./data`, `/data` in the containers)

```
videos/{video_id}/video.mp4          stimulus video as uploaded; served by the proxy with range requests
videos/{video_id}/aois.json          optional, written when AOIs are saved in the admin
studies/{study_id}/sessions/{session_id}/
  raw/
    chunks/{stream}/{seq:06d}.bin    chunks exactly as the browser sent them
    events.jsonl                     assembled from the events chunks when the session completes
    browser_features.jsonl           assembled from the features chunks (only if there are some)
    stimulus_frames.jsonl            assembled from the stimulus_frames chunks (only if there are some)
    webcam.webm                      the webcam chunks concatenated and remuxed (written by ingest)
    meta.json                        session row + config snapshot
  derived/
    webcam_frames.parquet            one row per webcam frame, from ingest (below)
    landmarks.parquet                server-side MediaPipe landmarks of every webcam frame (below)
    features_a.parquet               Pipeline A features of every webcam frame (below)
    sync.json                        webcam frame time to client time (below)
    calibration_a.json               the fitted calibration model of Pipeline A (below)
    gaze_a.parquet                   the gaze of every webcam frame (below)
    fixations_a.parquet              the fixations of the playback phase (below)
    quality.json                     the session quality report and inclusion decision (below)
```

Every file is written to a temporary name in the same directory and renamed, so a reader never sees a partial file. Raw files are never modified after they are written, except `meta.json` (see below).

`webcam.webm`, `events.jsonl`, `browser_features.jsonl` and `stimulus_frames.jsonl` are pure functions of the chunks. The ingest stage (`backend/worker/pipeline/ingest.py`) checks the chunks against the `chunks` table (every seq from 0 to the highest is there, size and sha256 match), then writes the files that are missing and refuses (error, nothing overwritten) when one that exists differs from what the chunks give. `webcam.webm` is always rewritten (atomically): `ffmpeg -c copy -fflags +genpts` into a real file, so the result has a duration and a seek index (cues), which the MediaRecorder stream lacks. Everything else under `derived/` comes with the later stages of Milestone 3.

### `derived/webcam_frames.parquet` (ingest)

One row per video packet of `raw/webcam.webm`, which is one per shown frame. The frame rate is variable; never assume it is constant.

| column | type | meaning |
|---|---|---|
| `index` | int64 | 0-based frame number |
| `pts` | float64 | presentation time in seconds from the start of the recording |

Snappy-compressed. The ingest summary also reports whether the pts are strictly increasing; the server-side landmark stage (`landmarks.parquet`) records the pts of the frames it decodes and is the reference for those.

### `derived/landmarks.parquet` (landmarks stage, PLAN §8.3)

One row per decoded frame of `raw/webcam.webm`, from the real MediaPipe Face Landmarker (Python, VIDEO mode, full resolution, blendshapes and transformation matrix on, up to 2 faces looked for and the first one kept, as in the browser). Code: `backend/worker/pipeline/landmarks.py`.

| column | type | meaning |
|---|---|---|
| `index` | int64 | 0-based frame number in decoding order |
| `pts` | float64 | presentation time in seconds (equals `webcam_frames.parquet`) |
| `face` | bool | a face was found |
| `lm` | fixed-size list of 1434 float32 | 478 landmarks x (x, y, z), flattened, normalised image coordinates; the mesh index `i` is at `3i..3i+2`, so the iris rings are 468 to 477. Null without a face. |
| `matrix` | fixed-size list of 16 float64 | facial transformation matrix, row-major, translation (cm) in indices 3, 7, 11. Null without a face. |
| `blink_l`, `blink_r` | float32 | `eyeBlinkLeft` / `eyeBlinkRight` blendshape scores. Null without a face. |

Schema metadata: `width` and `height` of the decoded frames (strings). Timestamps handed to MediaPipe are the pts in whole milliseconds, moved up by 1 ms where two frames would share one. The result of a frame depends slightly on the frames before it (the model tracks), and on the decoder's colour conversion: PyAV and OpenCV differ by about 0.01 in normalised units on the same clip, like the browser's output.

## Streams and chunks

Four stream names exist: `webcam`, `features`, `events`, `stimulus_frames`. A session uploads each stream as numbered chunks: `PUT /api/sessions/{id}/chunks/{stream}/{seq}`, raw bytes in the body, header `X-Chunk-SHA256` with the lowercase hex sha256 of the body, bearer upload token. Seqs start at 0 and are contiguous. The server verifies the hash, stores the bytes unchanged and records `(session, stream, seq, size, sha256)` in the `chunks` table. The same seq with the same bytes is accepted again (idempotent); the same seq with other bytes is refused (409).

What each stream holds:

| stream | chunk content | chunk written by the browser |
|---|---|---|
| `webcam` | raw bytes of one continuous WebM (VP9, else VP8, else the browser's default) from `MediaRecorder`, about 2.5 Mbit/s. **Only chunk 0 has the WebM header**: the recording is the byte concatenation of the chunks in seq order. | every `recording.chunk_ms` (the recorder's timeslice) |
| `features` | gzip of JSON lines, one per frame the live face model processed (below) | every `recording.chunk_ms`, none for an empty interval |
| `events` | gzip of JSON lines (below) | same |
| `stimulus_frames` | gzip of JSON lines, one per presented frame of the stimulus video (below) | same |

The server refuses (422, nothing stored) a `features`, `events` or `stimulus_frames` chunk that is not one complete gzip stream or inflates to more than `MAX_EVENTS_CHUNK_INFLATED_MB`, and a first `webcam` chunk (seq 0) that does not start with the WebM magic bytes `1A 45 DF A3`. Other webcam chunks are not inspected.

## The `events` stream

Each chunk is the **gzip** of newline-terminated JSON lines. The browser starts a new chunk every `recording.chunk_ms` (default 2000 ms) and sends none for an empty interval. The sha256 is over the compressed bytes. The server refuses (422) an events chunk that is not one complete gzip stream or that inflates to more than `MAX_EVENTS_CHUNK_INFLATED_MB`. `raw/events.jsonl` is the gunzipped chunks concatenated in seq order (`backend/app/events.py`, also used by the later ingest stage); it is written when the session completes and the stream is valid.

One line:

```json
{"t": 1234.5, "type": "calib_dot_show", "data": {"phase": "calibration", "id": 0, "x": 640, "y": 360}}
```

- `t` is `performance.now()` in milliseconds on the participant's clock (PLAN §5.3); events are written in the order they happened.
- `type` is one of the names in `shared/protocol.json` (`event_types`). If a line is invalid or has an unknown type, the session still completes, is flagged `events_invalid`, and has no `raw/events.jsonl` (the chunks are kept).
- Coordinates are viewport CSS pixels.

| type | `data` |
|---|---|
| `session_start` | `client_info`: `user_agent`, `platform`, `hardware_concurrency`, `screen {width, height}`, `device_pixel_ratio`, `time_origin` (`performance.timeOrigin`), `wall_clock_ms` (`Date.now()` at the same moment). The server copies `client_info` into the session. |
| `viewport_change` | `viewport {width, height}`, `screen {width, height}`, `device_pixel_ratio`, `video_rect {x, y, width, height}` or `null`. Logged at start, on every resize, and when a video appears or disappears. `video_rect` is the letterboxed content rectangle of the video, not the element box. |
| `fullscreen_change` | `fullscreen` (bool) |
| `visibility_change` | `state` (`visible` / `hidden`) |
| `window_blur`, `window_focus` | `{}` |
| `calib_dot_show` | `phase` (`calibration` or `validation`), `id` (index in shown order, restarts at 0 per block), `x`, `y` |
| `calib_dot_click`, `calib_dot_hide` | `phase`, `id` |
| `video_load` | `video_id`, `duration` (s), `width`, `height`, `rect` (content rectangle) |
| `video_play`, `video_pause`, `video_stall` | `video_id`, `media_time` (s) |
| `video_ended` | `video_id` |
| `client_error` | `message`, `stack` (or `null`), `url` |
| `recorder_start` | `mime_type`, `bits_per_second`, `settings` (the camera's `track.getSettings()` as the browser reports it: actual width, height, frame rate). Logged from the recorder's `start` event, a moment after the recording begins. |
| `recorder_dataavailable` | `seq`, `size`: one per webcam chunk, after it was stored in the browser |
| `recorder_stop` | `chunks`, `bytes`: totals of the recording, after the last chunk was stored |
| `face_lost` | `{}`: no face for at least 500 ms while recording; `t` is when this was confirmed |
| `face_regained` | `lost_ms`: the face was back for 500 ms; `lost_ms` is the length of the loss from the first frame without a face |
| `calib_gate_result` | `block` (`before_video`, `between_videos`, `after_last_video`), `attempt` (1-based; later blocks use 1), `passed` (`true`, `false`, or `null` when skipped), `skipped`, `mean_error_norm` (or `null`), `point_errors` (per validation point, normalised by the viewport diagonal; `null` for a point without a usable frame), `valid_frames_per_dot` (per calibration dot, in dot order), `reason` (`ok`, `few_face_frames`, `low_brightness`, `head_or_distance_change`, `high_error`, `no_validation`, `forced_debug`). Only `before_video` results can block the participant; later blocks are logged for the quality report. With `calibration_gate.enabled: false` no `calib_gate_result` is logged at all (neither for the pre-video block nor for later ones) and the session is never flagged `calibration_gate_failed`. |
| `debug_skip` | `target` (`calibration`, `validation`, `video`): debug mode `S` |

A dot that was interrupted (fullscreen left) and restarted has two `calib_dot_show` events with the same `id` (the first closed by a `calib_dot_hide`); consumers use the **last** show and click of each `(phase, id)`.

## The `features` stream and `raw/browser_features.jsonl`

One line per frame the live face model processed, at the study video's frame rate (the camera's during calibration and validation), reduced when the stimulus video drops frames. Only written while recording.

```json
{"t": 12345.6, "webcam_media_time": 41.233, "capture_time": 12340.1, "presented_frames": 1237, "face": true,
 "lm": [[x, y, z], ...49 landmarks], "matrix": [16 numbers], "blink_l": 0.03, "blink_r": 0.04,
 "face_ratio": 0.162, "brightness": 0.47}
```

- `t`: client clock (`performance.now()`, ms) when the webcam frame was presented to the page, from `requestVideoFrameCallback` on the hidden webcam preview. `webcam_media_time`, `capture_time` (or `null`) and `presented_frames` are that callback's metadata.
- `face`: `false` when no face was found; then `lm`, `matrix`, `blink_l`, `blink_r`, `face_ratio` and `brightness` are all `null`. If there were several faces, the first one is used. With a face, everything is set except that `brightness` may be `null` (the pixels could not be read).
- `lm`: 49 MediaPipe mesh landmarks (normalised image x, y, z; 5 decimals), the subset and order of `shared/landmark_subset.json` (both iris rings, eye corners and lids, nose tip, chin, forehead, cheeks).
- `matrix`: the facial transformation matrix, **row-major** (translation in indices 3, 7, 11, in cm). The browser API delivers it column-major; the app converts it.
- `blink_l`, `blink_r`: the `eyeBlinkLeft` / `eyeBlinkRight` blendshape scores. `face_ratio`: distance between the two eye centres as a fraction of the frame width. `brightness`: mean luminance of the face bounding box, 0 to 1.
- The Pipeline A features (iris position in the eye-local frame, eye openness, head yaw/pitch/roll) are computed from `lm` and `matrix` and the frame's aspect ratio by `backend/worker/pipeline/features_a.py` and `frontend/src/face/features.ts`; their definition is in those files' headers and in DECISIONS ("Milestone 2 T3").

`raw/browser_features.jsonl` is the gunzipped chunks concatenated in seq order, written at `/complete` when every line is valid. If not, nothing is written, the chunks are kept and the session is flagged `features_invalid`.

## The `stimulus_frames` stream and `raw/stimulus_frames.jsonl`

One line per presented frame of the stimulus video (`requestVideoFrameCallback`), the authoritative mapping from the client clock to the video position:

```json
{"t": 20001.25, "expected_display_time": 20016.5, "media_time": 1.2, "presented_frames": 36}
```

`t` is the client clock when the callback ran; the other three are the callback's metadata (`media_time` in seconds). The record does not say which video it belongs to: the `video_load` / `video_ended` events delimit the videos, and `presented_frames` restarts with each video. Invalid lines give the flag `stimulus_frames_invalid`, as above.

## `raw/meta.json`

Written when the session is created and rewritten (atomically) when it completes.

```json
{
  "id": "3f2b8c1e-5d4a-4e9b-9c1a-2f6d7e8a9b0c",
  "study_id": 4,
  "participant_code": "AB0315",
  "created_at": "2026-10-13T07:41:02.123456+00:00",
  "status": "uploaded",
  "consent_version": "v1",
  "consent_at": "2026-10-13T07:41:09.000000+00:00",
  "is_debug": false,
  "flags": [],
  "client_info": { "...": "copied from the session_start event" },
  "config": { "...": "the study config at the time the session was created" }
}
```

- `config` is a snapshot of the study config (PLAN §4.1, every default filled in). It never changes afterwards, so later edits to the study do not change how an existing session is processed.
- `status`, `consent_*`, `flags` and `client_info` are empty or `created` in the first version and final in the second.
- `flags` can contain: `interrupted` (the participant reloaded the page mid-session and finished the upload from a later page load), `events_invalid` (the events stream could not be assembled, see above), `empty` (the session completed without any chunk), `features_invalid` and `stimulus_frames_invalid` (that stream could not be assembled), `calibration_gate_failed` (the last pre-video `calib_gate_result` has `passed: false`; derived by the server from the events).

## Session status

`created` (session made) → `consented` (consent recorded) → `recording` (first chunk accepted) → `uploaded` (`/complete` found no missing chunk). `processing`, `processed`, `failed` and `withdrawn` exist in the database but nothing sets them yet. Completing a session does not enqueue a job until Milestone 3.

### `derived/features_a.parquet` (features stage, PLAN §8.4)

One row per row of `landmarks.parquet`, computed from the 49-landmark subset of `shared/landmark_subset.json`, the matrix and the frame's aspect ratio by `frame_features` (`backend/worker/pipeline/features_a.py`, the Python twin of the browser's `features.ts`; definitions in that file's header).

| column | type | meaning |
|---|---|---|
| `index`, `pts`, `face` | | copied from `landmarks.parquet` |
| `iris_l_x`, `iris_l_y`, `iris_r_x`, `iris_r_y` | float64 | iris centre in the eye-local frame (origin at the middle of the eye corners, x along the corner-to-corner line, unit: eye width) |
| `open_l`, `open_r` | float64 | eye openness: middle lid distance over eye width |
| `yaw`, `pitch`, `roll` | float64 | head angles in degrees from the transformation matrix (positive pitch: head down; positive yaw: face turned to the image's right; positive roll: counter-clockwise) |
| `tx`, `ty`, `tz` | float64 | head translation in cm |
| `face_ratio` | float64 | distance between the two eye centres as a fraction of the frame width |
| `blink_l`, `blink_r` | float64 | `eyeBlinkLeft` / `eyeBlinkRight` scores |
| `blink` | bool | `blink_l > blink_score_threshold` or `blink_r > blink_score_threshold` or `open_l < blink_openness_threshold` or `open_r < blink_openness_threshold` (defaults 0.5 and 0.15; PLAN §8.4) |

Every float column is null in a frame without a face, where `blink` is false. Schema metadata: `config` holds the thresholds used, as JSON; `width` and `height` are copied from `landmarks.parquet`. The thresholds are fields of `PipelineConfig` (`backend/worker/pipeline/config.py`), which collects the plan's numbers for all stages.

### `derived/sync.json` (sync stage, PLAN §8.2)

Maps the time of a webcam frame (`pts`, seconds) to the client clock `t` (ms): `t = a_ms + 1000 * b * pts`. Code: `backend/worker/pipeline/sync.py`.

```json
{
  "version": 1,
  "config": { "...": "every PipelineConfig value used" },
  "recorder_start_ms": 4313.8,
  "lag_ms": -3.6, "peak": 0.998,
  "t_of_pts": { "a_ms": 4310.2, "b": 1.0 },
  "residual_max_ms": 0.0,
  "windows": [ { "pts_center": 5.6, "lag_ms": -3.6, "peak": 0.998, "used": true, "residual_ms": 0.0 } ],
  "curve": { "lag_ms": [-1000, -990, "..."], "r": [0.02, "...", null] },
  "flags": ["sync_short"],
  "frames": { "server": 340, "browser": 313 }
}
```

- The coarse map is `t = recorder_start_ms + 1000 * pts`. `lag_ms` is the offset from it that maximises the correlation of the browser's and the server's signals over the whole session, `peak` the correlation there (mean over the two channels of the Pearson correlation over the common valid samples). `curve` is the correlation at every lag searched (`null` where too little data overlapped), for the debug plot.
- The signals are the horizontal iris position in the eye-local frame (mean of both eyes) and the head yaw, z-scored, from `features_a.parquet` and from the browser's `raw/browser_features.jsonl` (run through the same `frame_features`); frames without a face and blinks are left out; both are resampled to 100 Hz, gaps over 150 ms are not bridged.
- `windows`: the same search in windows of 60 s (a rest shorter than 30 s joins the last one), each relative to the whole-session lag. `used` windows (peak at least 0.3 and not at the edge of the search) go into the Huber fit of lag against pts, which gives `a_ms` and `b`; `residual_ms` is a used window's distance from that line. A session with fewer than two full windows has one window, `b` is 1 and the lag is the offset.
- `flags` (the stage never fails on a weak result; the caller puts them on the session): `sync_short` (less than two windows), `sync_weak_peak` (whole-session peak under 0.3), `sync_lag_at_limit` (best lag at the edge of the +-1000 ms search), `sync_drift_high` (`|b - 1|` over 0.001), `sync_residual_high` (a window more than 40 ms off the line), `sync_drift_unknown` (fewer than two usable windows in a long session), `sync_coarse_only` (no usable browser or server frames: the coarse map with lag 0, `peak` null).

### `derived/calibration_a.json` (calibration stage, PLAN §8.6)

The per-session gaze model of Pipeline A, fitted on the calibration dots. Code: `backend/worker/pipeline/calibration.py` (fit and model choice) and `ridge.py` (the regression, the Python twin of the browser's `ridge.ts`).

```json
{
  "version": 1, "config": { "...": "every PipelineConfig value used" },
  "status": "ok", "failed_reason": null,
  "feature_columns": ["iris_l_x", "iris_l_y", "iris_r_x", "iris_r_y", "open_l", "open_r", "yaw", "pitch", "roll", "face_ratio"],
  "attempt_used": { "attempt": 2, "index": 1, "passed": true, "reason": "last_passed", "start_t": 31000.0 },
  "attempts": [ { "attempt": 1, "index": 0, "passed": false, "gate_reason": "high_error", "dots": 13 } ],
  "viewport": { "width": 1280.0, "height": 720.0 },
  "dots": [ { "id": 0, "x": 640.0, "y": 360.0, "click_t": 2000.0, "valid_frames": 24, "kept_frames": 22 } ],
  "samples": { "valid": 312, "kept": 301, "rejected": 11 },
  "cv": { "linear": { "degree": 1, "alpha": 10.0, "error_px": 61.2 },
          "quadratic": { "degree": 2, "alpha": 100.0, "error_px": 70.4 },
          "chosen": "linear", "error_px": 61.2, "error_norm": 0.0414 },
  "model": { "alpha": 10.0, "degree": 1, "means": [], "stds": [], "weights": [[], []], "intercepts": [0.0, 0.0] }
}
```

- **Attempts** are the calibration blocks of the session in order (a run of calibration-phase dots; a retry after a failed browser gate is a new block), each with the browser's `before_video` `calib_gate_result` that follows it. The attempt fitted is the last one that passed that gate (`last_passed`), else the last one (`none_passed_used_last`); a single attempt is `only_attempt`. A dot's position is its last `calib_dot_show`, its click time the last `calib_dot_click`.
- **Samples:** the frames of `features_a.parquet` with a face, no blink and every feature finite, from 700 ms before to 100 ms after the dot's click (client time through `sync.json`). `valid_frames` counts them; `kept_frames` those left after the outlier rejection. The regression input is the 10 `feature_columns`, the target the dot position in viewport px.
- **Model choice:** leave-one-dot-out cross-validation over alpha in 1e-3 .. 1e3 (one value per decade) for the degree-2 model and the plain linear model; `error_px` is the mean over the left-out dots of the mean per-frame distance to the dot; the lower one wins, a tie goes to the linear model. `error_norm` divides by the viewport diagonal (null without a viewport). Both errors are kept.
- **Outliers:** after a first fit, samples whose prediction is further than 2.5 MADs from their dot's median prediction (MAD = the median distance of that dot's predictions to their median) are dropped, and alpha, degree and the weights are chosen and fitted again on the rest.
- **`model`:** `StandardScaler` (population std, 0 replaced by 1), then for degree 2 the products `x_i * x_j` for `i <= j` after the original columns (scikit-learn's order), then ridge with an unpenalised intercept; `weights[0]` is x, `weights[1]` is y. The browser's `ridge.ts` and this file use the same definition and are tested against scikit-learn.
- **`status: "failed"`** (`model` null, nothing else is run for the session): `no_calibration_dots`, `too_few_valid_frames` (more than 3 dots with fewer than 5 valid frames), or `fit_impossible`. A failed calibration is a data problem, not a crash; the session is then excluded (DECISIONS milestone 3 item 15).

### `derived/gaze_a.parquet` (gaze stage, PLAN §7)

One row per webcam frame (the rows of `features_a.parquet`), in time order. Code: `backend/worker/pipeline/gaze.py`.

| column | type | meaning |
|---|---|---|
| `t` | float64 | client clock, ms (`a + 1000 b pts` of `sync.json`) |
| `webcam_pts` | float64 | seconds in `webcam.webm` |
| `video_id` | string, null | the stimulus video on screen (from its first presented frame to its `video_ended` event) |
| `stim_time` | float64, null | stimulus media time in seconds; null outside a video and in a pause or stall |
| `x_px`, `y_px` | float32 | raw prediction in viewport CSS px; NaN without usable features |
| `x_sm`, `y_sm` | float32 | after smoothing; NaN outside the smoother's segments |
| `x_vid`, `y_vid` | float32, null | `x_sm`, `y_sm` normalised to the video content rectangle (0 to 1 inside; outside it below 0 or above 1); null when no video is shown |
| `valid` | bool | usable features and the prediction within the viewport expanded by 15 % |
| `blink` | bool | the blink rule of `features_a.parquet` |
| `conf` | float32 | 0 for an invalid sample, 0.5 for an interpolated one, otherwise 1 minus a linear penalty for the prediction lying outside the viewport (1 inside it, 0 at the 15 % limit) |
| `phase` | string | `calibration` or `validation` inside a run of those dots, `playback` while a video is on screen, else `other` |

Added to the plan's columns: `interpolated` (bool: filled in by the smoother's gap interpolation; `valid` stays false) and the drift-corrected gaze `x_px_dc`, `y_px_dc`, `x_sm_dc`, `y_sm_dc`, `x_vid_dc`, `y_vid_dc` (the same columns with the drift of `validation` taken out; all null when no validation block gave data). The uncorrected columns are always kept. Schema metadata `gaze` (JSON) holds the config used, whether the drift correction was possible and the validation precision.

### `derived/fixations_a.parquet` (gaze stage, PLAN §8.9)

I-DT on the smoothed gaze (valid and interpolated samples) of the playback phase, per run of consecutive frames in one video and one viewport size. One row per fixation:

| column | type | meaning |
|---|---|---|
| `id` | int64 | 0-based |
| `start_t`, `end_t`, `duration_ms` | float64 | client clock of the first and last sample (duration = their difference) |
| `samples` | int64 | |
| `video_id` | string | |
| `stim_start`, `stim_end` | float64, null | media time at the first and last sample |
| `dispersion_px` | float64 | x range plus y range of the samples |
| `cx_px`, `cy_px` | float64 | centroid of the smoothed gaze, viewport px |
| `cx_vid`, `cy_vid` | float64, null | centroid in video-normalised coordinates |
| `cx_px_dc`, `cy_px_dc`, `cx_vid_dc`, `cy_vid_dc` | float64, null | the same, drift-corrected |

Parameters: the dispersion limit is `max(0.05 * viewport width, 2 * validation precision)`, the minimum duration 100 ms. Schema metadata `fixations` (JSON) holds the algorithm, these values (and the validation precision measured) and the whole config.

### Prediction, validity and smoothing (PLAN §8.7)

Not files of their own: `backend/worker/pipeline/predict.py` and `smoothing.py` produce the columns that `gaze_a.parquet` (Milestone 3, T9) writes.

- **`x_px`, `y_px`:** the calibration model applied to every frame with usable features (a face, no blink, every feature finite), in viewport CSS px; null/NaN for the other frames. The raw prediction is kept also when it lies outside the screen.
- **`valid`:** the frame is usable and the prediction is inside the viewport expanded by 15 % of its width and height on every side (`valid_viewport_margin`). The viewport is the size of the last `viewport_change` event before the frame (the first one for earlier frames).
- **Interpolation:** invalid samples between two valid ones whose times are at most 150 ms apart (`smooth_max_interp_gap_ms`; four lost frames at 30 fps are 167 ms, three are 133 ms) are filled by linear interpolation of x and y over time. They are marked `interpolated` and stay `valid = false`. Nothing is extrapolated before the first or after the last valid sample.
- **Segments:** runs of valid and interpolated samples; a longer gap starts a new segment, and segments are smoothed independently.
- **Smoothing (`x_sm`, `y_sm`):** a Rauch-Tung-Striebel (forward-backward Kalman) smoother per segment with a constant-velocity model, x and y independent, measurement noise 15 px, white-noise acceleration density 3e7 px^2/s^3, velocity at the start of a segment unknown (2000 px/s). Interpolated samples count as measurements. Not smoothed (NaN): samples in no segment.

### `derived/quality.json` (quality stage, PLAN §8.10)

Written last, by `backend/worker/pipeline/quality.py`; the admin quality report shows it and the runner copies it to `sessions.quality`. Sections:

| key | content |
|---|---|
| `session_flags`, `is_debug` | from `raw/meta.json` |
| `webcam` | `frames`, `duration_s`, `fps_mean`, `fps_median`, `gaps` (steps over 100 ms), `max_gap_ms`, from `webcam_frames.parquet` |
| `face_lost` | `all` and `playback`: `episodes`, `total_ms`, `longest_ms` of stretches without a face that last at least 500 ms (from the first frame without a face to the first with one), from the server's own detections; `playback` is null without gaze |
| `events` | `fullscreen_exits` (while recording), `fullscreen_exits_in_playback`, `stalls` (a `video_stall` with media time over 0), `stalls_at_start`, `page_left` (page hidden or window blurred), `browser_face_lost_episodes` (the browser's `face_lost` events) |
| `playback` | frames with the video on screen, `valid_frames`, `valid_fraction`, `blink_fraction`, `interpolated_fraction`, and `per_video`; null with a failed calibration |
| `face_ratio` | median face ratio during the calibration dots, the study's two warn multipliers, and `playback_share_outside`: the share of playback frames whose face ratio is outside `[min_face_ratio_warn, max_face_ratio_warn]` times that median |
| `browser_gate` | every `calib_gate_result` the browser logged: `block`, `attempt`, `passed`, `skipped`, `mean_error_norm`, `reason` |
| `calibration` | `status`, `failed_reason`, `attempt_used`, `attempts`, `cv` and `samples` of `calibration_a.json` |
| `validation` | the blocks, drift and `precision_px` of the validation stage (above) plus `mean_error_norm` and `mean_error_corrected_norm` (mean over the blocks with data); null with a failed calibration |
| `sync` | `lag_ms`, `peak`, `t_of_pts`, `residual_max_ms`, `flags` of `sync.json` |
| `inclusion` | `included`, `reasons`, `criteria` (below) |
| `stages` | per stage its duration and summary, put in by the runner |

**Inclusion** uses the study's `inclusion` thresholds (the config snapshot in `raw/meta.json`; a value equal to its threshold passes). The session is left out, with every reason listed, when: it is a debug session (`debug_session`); the calibration failed (`calibration_failed`, then nothing else is judged); there is no playback (`no_playback_data`) or its valid fraction is below `min_valid_fraction` (`valid_fraction_low`); there is no validation data (`no_validation_data`) or the mean `error_norm` over the validation blocks (uncorrected) is above `max_validation_error_norm` (`validation_error_high`). Sync flags are reported but do not exclude a session.

### Validation accuracy, precision and drift (PLAN §8.8)

Computed by `backend/worker/pipeline/validation.py`; the results are part of `quality.json` (Milestone 3, T10) and the corrected columns of `gaze_a.parquet` (T9). One entry per **validation block**: a run of `phase: validation` dots, from the calibration attempt that was used onwards (blocks of earlier, failed attempts are not scored). `kind` is `before_video`, `between_videos` or `after_last_video`, found from the `video_load` / `video_ended` events.

For each point, the raw predictions of the valid frames from 700 ms before to 100 ms after the dot's click (the training window):

- `error_px`: distance between the median prediction (median of x and of y) and the target; `error_norm`: that over the viewport diagonal at the click; `error_deg`: always null (needs a station, which does not exist yet).
- `precision_px`: RMS of the distance between consecutive frames' predictions (only frames that directly follow each other); null with no such pair.
- `error_corrected_px` / `error_corrected_norm`: the error after the drift at the click time is taken out (below).
- A point without a valid frame has all of these null and does not count in the block's mean.

Per block: `mean_error_px`, `mean_error_norm`, `mean_error_corrected_*`, `precision_px` (mean over the points), `offset_px` (mean over the points of the median prediction minus the target), `t_ms` (mean click time of the points with data) and `points_with_data`. Session level: `precision_px` is the median over the blocks (the input of the fixation dispersion threshold, §8.9) and `drift` lists, for the blocks with data, `t_ms`, `dx_px`, `dy_px`.

**Drift correction:** the offset at a time is the linear interpolation of the blocks' offsets between their `t_ms`, constant before the first and after the last block (with one block, constant). Subtracting it from `x` and `y` gives the drift-corrected gaze; the uncorrected columns are always kept. The first block (right after the calibration) is included, so the correction also removes the calibration's own constant bias.

### Client time to stimulus time (PLAN §8.2 step 5)

Not a file: `backend/worker/pipeline/stimulus_map.py` maps client times to `(video_id, stim_time)` from `raw/stimulus_frames.jsonl` and the `video_load` / `video_ended` events (the frame records carry no video id). A time between two presented frames gets the media time linearly interpolated between them. `stim_time` is null in a gap: a step between two frames where the wall clock advanced more than 50 ms more than the media time (pause, stall) or the media time went back (seek); dropped frames advance both and are not gaps. A video is on screen from its first presented frame until its `video_ended` event (or one frame after its last frame); after its last frame only one frame duration is extrapolated. Outside a video both are null.

## Participant code

Two ASCII letters, then birth month and day, for example `AB0315`: `^[A-Z]{2}(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])$`. Input is upper-cased. This is derived from personal data and can collide; see DECISIONS.
