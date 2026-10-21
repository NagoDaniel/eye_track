# Data formats

What the system writes to disk and sends over the wire, as of Milestone 5 (processing pipelines A and B, analysis). Sections are added as later milestones write more files (PLAN §7). The reference for names and payload shapes is `shared/protocol.json`; `backend/app/schemas.py` and `frontend/src/{config,eventLog,records}.ts` mirror it and tests compare all three.

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
    gaze_a.parquet                   the gaze of every webcam frame, Pipeline A (below)
    fixations_a.parquet              the fixations of the playback phase, Pipeline A (below)
    quality.json                     the session quality report and inclusion decision (Pipeline A)
    stages.json                      per stage: status (ok, skipped, failed), seconds, summary, error; written after every stage
    features_b.parquet               Pipeline B features (gaze angles of the neural network + head pose), only after a Pipeline B run
    calibration_b.json               the fitted calibration model of Pipeline B (same format as A, six features)
    gaze_b.parquet                   the gaze of every webcam frame, Pipeline B (same columns as A)
    fixations_b.parquet              the fixations of the playback phase, Pipeline B
    quality_b.json                   Pipeline B's quality report and inclusion decision
    stages_b.json                    the stages of Pipeline B, as stages.json
    pipeline_b_stale.json            exists only while Pipeline B's files no longer fit Pipeline A's (below)
    debug/calibration.png, validation.png, sync.png, signals.png, gaze.png        debug plots of Pipeline A
    debug/calibration_b.png, validation_b.png, gaze_b.png                          debug plots of Pipeline B
    debug/replay.mp4                 the debug replay video, only when rendered on request (below)
    intermediates/webcam_concat.webm only with cli.py process --keep-intermediates
analysis/{study_id}/{video_id}/{run_id}/  run.json and the analysis outputs (below)
```

Every file is written to a temporary name in the same directory and renamed, so a reader never sees a partial file. Raw files are never modified after they are written, except `meta.json` (see below).

`webcam.webm`, `events.jsonl`, `browser_features.jsonl` and `stimulus_frames.jsonl` are pure functions of the chunks. The ingest stage (`backend/worker/pipeline/ingest.py`) checks the chunks against the `chunks` table (every seq from 0 to the highest is there, size and sha256 match), then writes the files that are missing and refuses (error, nothing overwritten) when one that exists differs from what the chunks give. `webcam.webm` is always rewritten (atomically): `ffmpeg -c copy -fflags +genpts` into a real file, so the result has a duration and a seek index (cues), which the MediaRecorder stream lacks. The other files under `derived/` are written by the later stages; their schemas are in "Derived files of the pipelines" below.

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

`created` (session made) → `consented` (consent recorded) → `recording` (first chunk accepted) → `uploaded` (`/complete` found no missing chunk and queued the `process_session` job) → `processing` (the worker runs the stages) → `processed` (the quality report exists) or `failed` (a stage raised; the job's log names the stage). `withdrawn` exists in the database but nothing sets it yet.

`/complete` queues the job once per session (a repeated `/complete` does not queue another) and not for a session without any chunk (flag `empty`). Job kinds for a session: `process_session` (the stages of Pipeline A, above), `process_pipeline_b` (the stages of Pipeline B; it needs a processed session, does not change its status and leaves A's files alone) and `render_debug` (the replay video; it does not change the session's status). A session can have only one open job of these kinds at a time. A processed or failed session can be processed again from any stage: a `process_session` job with `from_stage` or `only` in its args (the CLI and the admin page do this). A worker that dies mid-job leaves the session `processing`; the next worker start puts the job back in the queue and the session back to `uploaded`, or fails both after `JOB_MAX_ATTEMPTS` (DECISIONS milestone 3 item 14).

The worker sets these session flags (and removes them when a later run no longer finds the problem), next to the ones of the participant app: `sync_short`, `sync_weak_peak`, `sync_lag_at_limit`, `sync_drift_high`, `sync_residual_high`, `sync_drift_unknown`, `sync_coarse_only` (see `sync.json`) and `calibration_failed` (see `calibration_a.json`). `sessions.quality` holds `quality.json` and, once Pipeline B has run, the extra key `pipeline_b` (see "`derived/quality.json`").

## Session archive (`cli.py export-session` / `import-session`, PLAN §12.3)

A ZIP that makes a field problem reproducible on a development machine (`backend/app/session_archive.py`):

```
meta.json      the session row and study-config snapshot (the session's raw/meta.json)
chunks.json    the session's rows of the chunks table: [{"stream", "seq", "size", "sha256"}, ...]
raw/...        the raw files without meta.json (chunks, assembled streams, webcam.webm)
derived/...    everything the stages wrote
```

`--no-raw-video` leaves out `raw/webcam.webm` and `raw/chunks/webcam/`; `chunks.json` still lists those chunks. Import refuses paths outside `raw/` and `derived/`, checks every chunk file against `chunks.json` (a missing webcam chunk is allowed), creates the study (`imported-<id>`) from the config snapshot if its id is not there, and creates the session with an unusable upload token. The status is `processed` when `derived/quality.json` exists, else `uploaded`; `raw_video_deleted_at` is set when there is no webcam. An existing session needs `--force`: the archive is extracted and verified in a staging directory first, and only then is the existing session replaced; if anything fails the old session stays as it was.

`cli.py process --keep-intermediates` keeps `derived/intermediates/webcam_concat.webm`, the webcam chunks concatenated before the remux (also when ffmpeg failed); a run without the flag removes it.

## `videos/{id}/aois.json` (PLAN §9.3)

Mirrors `videos.aois`. Validated when the admin saves it (`backend/app/aois.py`, 422 with the reason); unknown keys are refused. Coordinates are video-normalized (0 to 1 over the content rectangle), times are stimulus media seconds.

```json
{
  "segments": [{"id": "intro", "start": 0.0, "end": 32.5}],
  "aois": [
    {"id": "code_panel", "label": "Code", "start": 12.0, "end": 95.0,
     "shape": "rect", "coords": [0.55, 0.10, 0.98, 0.90]},
    {"id": "dancers", "label": "Dancers", "start": 0.0, "end": 180.0,
     "shape": "polygon", "coords": [[0.1, 0.2], [0.5, 0.2], [0.5, 0.9], [0.1, 0.9]]}
  ]
}
```

`rect` coords are `[x0, y0, x1, y1]` with x1 > x0, y1 > y0; `polygon` has at least 3 `[x, y]` points (concave allowed). Both lists default to empty; ids are unique within their list; `end` is after `start`. An AOI is active from `start` up to but excluding `end`. A point on the border is inside. The hit tolerance grows the shape by a distance (round corners); the analysis gives it in video widths and corrects the vertical direction by the video's aspect ratio, so the growth is the same on screen both ways.

## `analysis/{study_id}/{video_id}/{run_id}/run.json` (PLAN §9)

One `analyze_video` run (`backend/worker/analysis/run.py`), started from the admin or with `docker compose exec api python cli.py analyze <study_id> <video_id> [--as-measured] [--tolerance-k K]`. The directory holds `run.json` and the files below; a stage that fails leaves the finished stages' files in place and the run `failed`. A session whose gaze cannot be read is listed in `excluded` (`gaze_unreadable: ...`), one without a valid sample on the video as `no_valid_samples`; a run with nobody left fails (`no included sessions`). `results` has the sigma used and, per stage, its summary and seconds. `run_id` is `YYYYMMDDTHHMMSSZ-xxxxxx` (UTC start time, random tail); a run directory is never reused for a finished run. A job that is requeued after its worker died (same id) removes what the unfinished attempt left and starts again; in the admin list a `running` record whose job failed shows as `failed`. `run.json` is written when the run starts (`status` `running`) and again when it ends (`done`, or `failed` with `error`), so a run that died shows as `running`.

| key | meaning |
|---|---|
| `version`, `kind` | 1, `analyze_video` |
| `run_id`, `study_id`, `video_id` | |
| `status`, `error`, `started_at`, `finished_at` | ISO 8601 UTC |
| `git_hash` | `git describe --always --dirty` of the build (`GIT_SHA` build argument); `null` when the image was built without it |
| `parameters` | every value of `AnalysisConfig` that was used (`pipeline` `a` or `b`, `gaze_columns` `drift_corrected` (default) / `plain`, heatmap window, sigma floor, grid, dots on the heatmap video, segment length, AOI tolerance k, synchrony window) |
| `included` | `{session_id, participant_code, ...}` per session analysed (later stages add their numbers) |
| `excluded` | `{session_id, participant_code, reasons}` per session left out. Reasons: `not_processed`, `video_not_watched`, the reasons of the session's inclusion decision (`quality.json` for pipeline A, B's own in `quality_b.json` for pipeline B), for B also `pipeline_b_not_run` and `pipeline_b_stale`, `gaze_file_missing`, `superseded` (an older session of a participant who has a later included one) |
| `outputs` | files written, relative to the run directory |
| `results` | numbers the stages report |

Sessions that never finished uploading and withdrawn ones are not listed. One participant code counts once: its latest included session.

### `heatmap.mp4` and `density.npz` (PLAN §9.1)

Written by `backend/worker/analysis/heatmap_video.py` into the run directory. `heatmap.mp4` is the stimulus video (same frames, size cropped to even sides, frame rate) with the group density drawn on it: H.264, yuv420p, faststart; hotter is warmer (Turbo colour map) and more opaque, up to `heatmap_alpha_max`. One colour scale serves the whole video: the `heatmap_scale_percentile` (99) of the non-zero densities of all frames is fully hot. With `heatmap_dots` on, one white dot per participant is drawn at the median of their samples in the window.

For stimulus frame `k` (media time `s = k / fps`) the density uses the valid samples of every included participant with `stim_time` in `[s - heatmap_window_s, s + heatmap_window_s]` (video-normalized coordinates, from the plain or the drift-corrected columns as the run says). A participant's density is the mean of Gaussians at their samples, so each participant weighs the same; the group density is their sum. The Gaussian is isotropic in video-normalized units with `sigma` = the median over included sessions of the mean validation accuracy in px divided by the width of the video's content rectangle on that participant's screen, at least `sigma_min` (DECISIONS 2026-10-19 item 3).

`density.npz`: `density` float32 `(frames, 36, 64)` (row = y from the top, column = x; cell centres at `(i + 0.5) / 64`, `(j + 0.5) / 36` of the video), and scalars `fps`, `sigma`, `scale` (the density drawn fully hot), `window_s`.

### `static/NNN_<id>.png` (PLAN §9.2)

Written by `backend/worker/analysis/heatmap_static.py`, one picture per time segment, numbered in segment order (the id is made file-name safe). The segments are the `segments` of the video's `aois.json`, or consecutive windows of `segment_default_s` (10) seconds over the whole video, the last one shorter; a segment is cut at the end of the video and one that starts after it is dropped (if all do, the stage fails). Segments are half-open `[start, end)` in media time. The density is the one of the dynamic heatmap (same sigma, equal weight per participant) over all samples of the participant in the segment, drawn over the video frame at the segment's middle. Each picture has its own colour scale (the 99th percentile of its non-zero densities).

### `aoi_metrics_long.csv` and `aoi_metrics_wide.csv` (PLAN §9.3)

Only written when the video has AOIs. Code: `backend/worker/analysis/aoi_metrics.py`. Per included participant and AOI, over the time the AOI exists (`start` to `end`, cut at the end of the video); coordinates are video-normalized and the shape is grown by `aoi_tolerance_k` times the participant's own validation accuracy, as a distance on screen (the accuracy in px, the same horizontally and vertically, with round corners; no growth when the accuracy is unknown). The long file has the columns `participant_code, aoi_id, metric, value`; the wide file has one row per participant and AOI and one column per metric. An empty value means "not defined" (no fixation hit the AOI).

| metric | meaning |
|---|---|
| `dwell_fixation_s` | time of the fixations whose centroid is in the shape; a fixation that starts before or ends after the AOI counts for the part inside the active time |
| `dwell_sample_s` | time of the valid samples in the shape; a sample stands for the time to the next webcam frame, at most `sample_max_dt_ms` (100) |
| `share_active` | `dwell_sample_s` over the AOI's active time (empty when the AOI starts after the video ends) |
| `time_to_first_fixation_s` | from the AOI's start to the start of the first hitting fixation (0 when it was already there); empty when none |
| `fixation_count` | number of hitting fixations |
| `revisits` | entries minus one (at least 0); an entry is a run of consecutive hitting fixations among the fixations in the active time |

### `synchrony.csv`, `synchrony_nss.csv`, `synchrony.png` (PLAN §9.4)

Code: `backend/worker/analysis/synchrony.py`. Windows of `synchrony_window_ms` (200) from 0 to the end of the video, `[start, start + 200 ms)`, the last one partial. In each window every included participant with valid samples has a median position (coordinate-wise). A window needs at least two such participants; otherwise `dispersion` and NSS are empty.

- `dispersion`: the mean distance of the participants' medians to their coordinate-wise group median, in units of the video's width (the vertical distance is multiplied by height over width). 0 = all look at the same place.
- NSS: for a participant, the leave-one-out group density (the other participants' densities of the heatmap, same sigma, summed) is z-scored over the 64x36 grid and averaged at the participant's samples in the window (a sample outside the video counts at the nearest edge cell). Positive = looking where the others look.

`synchrony.csv`: `window_start_s, window_end_s, participants` (with samples), `dispersion`, `nss_mean` (mean over the participants that have an NSS). `synchrony_nss.csv`: `participant_code, window_start_s, nss`. `synchrony.png`: both series over the video's timeline. The per-participant mean NSS is in `run.json` under `results`.

## Derived files of the pipelines (PLAN §7, §8)

What the stages of Pipeline A (`process_session`, stages `ingest` to `plots`) and of Pipeline B (`process_pipeline_b`, stages `features_b` to `plots_b`) write under `derived/`. The files of ingest and of the landmarks stage are described at the top of this document, the analysis outputs further up. Code: `backend/worker/pipeline/`. A session is one directory `studies/{study_id}/sessions/{session_id}/derived/`; the examples below set `d` to it.

Conventions shared by all files:

- **Client time** `t` is milliseconds on the participant's `performance.now()` clock, the clock of `events.jsonl`. A webcam frame with presentation time `pts` (seconds in `webcam.webm`) is at `t = a_ms + 1000 * b * pts` with `a_ms`, `b` from `sync.json`; the gaze tables carry the result in their column `t`.
- **Pixels** (`*_px`) are viewport CSS pixels, origin top left, y downwards, in the viewport that was in force at the frame. **Video-normalised** (`*_vid`) coordinates are fractions of the video's letterboxed content rectangle, 0 to 1 inside it, and can lie outside.
- **Null and NaN.** Where the text says "null" the Parquet value is null (pandas and R read it as `NaN` / `NA`). Four columns of the gaze tables (`x_px`, `y_px`, `x_sm`, `y_sm`) hold a real `NaN` instead; both read as `NaN` in pandas, but `arrow` in R tells them apart.
- Every JSON file has `version` (1) and `config`, the whole `PipelineConfig` (`backend/worker/pipeline/config.py`, every threshold with its default) as used by the run; Parquet files carry the parameters of the run in their schema metadata, under the key named in the section.
- The per-frame tables (`features_*`, `gaze_*`) have one row per webcam frame, in frame order; `index` and `pts` are those of `webcam_frames.parquet`.

```python
import json, pandas as pd, pyarrow.parquet as pq
d = "data/studies/4/sessions/3f2b8c1e-5d4a-4e9b-9c1a-2f6d7e8a9b0c/derived"
meta = json.loads(pq.read_schema(f"{d}/gaze_a.parquet").metadata[b"gaze"])   # schema metadata
```

### `derived/features_a.parquet` (stage `features`, PLAN §8.4)

One row per row of `landmarks.parquet`. Computed from the 49-landmark subset and the facial transformation matrix (definitions in the header of `features_a.py`; the browser computes the same numbers). "Left" and "right" are the subject's, as MediaPipe names them. In a frame without a face (`face` false) every column from `iris_l_x` to `blink_r` is null and `blink` is false.

| column | type | meaning |
|---|---|---|
| `index`, `pts` | int64, float64 | as in `landmarks.parquet`; `pts` in seconds |
| `face` | bool | a face was found |
| `iris_l_x`, `iris_l_y`, `iris_r_x`, `iris_r_y` | float64 | mean of the five iris landmarks in the eye-local frame: origin at the midpoint of the eye corners, x along the corner-to-corner vector (towards the image's right for both eyes), y a quarter turn towards the image's bottom, unit = the eye width |
| `open_l`, `open_r` | float64 | eye openness: distance between the middle upper and lower lid landmarks over the eye width (open eyes about 0.27 to 0.39, blinks 0.07 to 0.14 in the face test clip) |
| `yaw`, `pitch`, `roll` | float64 | head angles in degrees from the transformation matrix; positive pitch = head tilted down, positive yaw = face turned towards the image's right, positive roll = counter-clockwise in the image |
| `tx`, `ty`, `tz` | float64 | head translation from the matrix, cm |
| `face_ratio` | float64 | distance between the two eye midpoints as a fraction of the frame width (face size) |
| `blink_l`, `blink_r` | float64 | the `eyeBlinkLeft` / `eyeBlinkRight` blendshape scores, 0 to 1 |
| `blink` | bool | the blink rule below |

**Blink rule:** `blink = blink_l > blink_score_threshold or blink_r > blink_score_threshold or open_l < blink_openness_threshold or open_r < blink_openness_threshold`, with defaults 0.5 and 0.15 (the second is a starting value to tune on pilot data). The limits themselves are not blinks. Later stages treat a blink frame like a frame without a face (no gaze, not valid).

Schema metadata: `config` (JSON, the `PipelineConfig` used, so the two blink thresholds), `width` and `height` (strings; frame size, needed to scale x to the aspect ratio).

```python
f = pd.read_parquet(f"{d}/features_a.parquet")
f[f.face & ~f.blink][["pts", "iris_l_x", "iris_l_y", "yaw"]].describe()
```

### `derived/sync.json` (stage `sync`, PLAN §8.2)

Maps webcam time to the client clock. The server's features (horizontal iris position, mean of both eyes, and head yaw; z-scored; frames without face and blinks left out) are cross-correlated at `sync_rate_hz` (100) with the same signals computed in the browser (`raw/browser_features.jsonl`), over a lag of plus or minus `sync_max_lag_ms` (1000) around the coarse map `t = recorder_start_ms + 1000 * pts`; the two channels are correlated one by one and the correlations averaged.

| key | meaning |
|---|---|
| `version`, `config` | 1; the `PipelineConfig` used |
| `recorder_start_ms` | client time of the first `recorder_start` event (the coarse offset) |
| `lag_ms` | whole-session offset from the coarse map: the shift that aligns the signals. Positive: the frame is later on the client clock than `recorder_start_ms + 1000 * pts`. Refined with a parabola through the peak, so not a multiple of 10 ms |
| `peak` | the correlation at that lag (-1 to 1); `null` when nothing could be correlated |
| `t_of_pts` | `{"a_ms", "b"}`: the result, `t = a_ms + 1000 * b * pts`. `a_ms = recorder_start_ms + lag_ms` and `b = 1` unless the drift fit (below) was done, which replaces both |
| `residual_max_ms` | largest distance of a used window from the drift line (0 when no fit was done) |
| `windows` | one entry per window of the drift fit (windows of `sync_window_s` = 60 s over the recording; a last one shorter than `sync_min_window_s` = 30 s is merged into the one before): `pts_center` (s), `lag_ms` (the offset from the coarse map found in this window, so it includes the whole-session `lag_ms`), `peak`, `used` (peak at least `sync_min_peak_correlation` and not at the edge of the search; only these enter the fit), `residual_ms` (distance from the fitted line; `null` for unused windows). A Huber fit of `lag_ms` against `pts_center` gives the line: `a_ms` is its value at pts 0 plus `recorder_start_ms`, `b = 1 + slope / 1000` |
| `curve` | `{"lag_ms": [...], "r": [...]}`: the whole-session correlation for every lag (201 values with the defaults: -1000 to 1000 ms in 10 ms steps); `r` is `null` where too few samples overlapped |
| `frames` | `{"server": n, "browser": n}`: frames that had a usable signal on each side |
| `flags` | list, below |

Flags (also copied to the session's flags; none blocks the session):

| flag | set when |
|---|---|
| `sync_coarse_only` | fewer than 2 usable frames on either side: no correlation was possible; `lag_ms` 0, `b` 1, `a_ms = recorder_start_ms`, `windows` and `curve` empty |
| `sync_weak_peak` | `peak` below `sync_min_peak_correlation` (0.3) |
| `sync_lag_at_limit` | the best lag is at the edge of the search (the true lag may be beyond it) |
| `sync_short` | the recording is shorter than two windows (120 s): one window, no drift is estimated (`b` = 1) |
| `sync_drift_unknown` | several windows but fewer than 2 are `used`: no drift fit, `b` = 1 |
| `sync_residual_high` | `residual_max_ms` above `sync_max_residual_ms` (40) |
| `sync_drift_high` | `abs(b - 1)` above `sync_max_slope_error` (0.001) |

```python
sync = json.load(open(f"{d}/sync.json")); a, b = sync["t_of_pts"]["a_ms"], sync["t_of_pts"]["b"]
t_ms = a + 1000 * b * f.pts            # client time of every webcam frame
```

### `derived/calibration_a.json` and `calibration_b.json` (stages `calibration`, `calibration_b`; PLAN §5.5, §8.6)

The gaze model of one session. A and B use the same format and the same code; they differ only in the features the model is fitted on (`feature_columns`): A takes the ten columns `iris_l_x, iris_l_y, iris_r_x, iris_r_y, open_l, open_r, yaw, pitch, roll, face_ratio` of `features_a.parquet`; B takes the six columns `gaze_pitch, gaze_yaw, yaw, pitch, roll, face_ratio` of `features_b.parquet`. Training samples are the usable frames (a face, no blink, all features finite) from `calib_window_before_click_ms` (700) before a dot's click to `calib_window_after_click_ms` (100) after it, in client time; the target is the dot's position in viewport px. Of several calibration attempts the last one that passed the browser gate is used, else the last one.

| key | meaning |
|---|---|
| `version`, `config` | 1; the `PipelineConfig` used |
| `status` | `ok` or `failed` |
| `failed_reason` | `null` when `ok`, else `no_calibration_dots` (no calibration dot event), `too_few_valid_frames` (more than `calib_max_sparse_dots` = 3 dots have fewer than `calib_min_frames_per_dot` = 5 usable frames) or `fit_impossible` (the cross-validation cannot be computed, e.g. fewer than two dots with samples). A failed calibration is a data problem, not an error: no model, the gaze stage is skipped and its files are removed, the session gets the flag `calibration_failed` and is excluded |
| `feature_columns` | the model's input columns, in order |
| `attempt_used` | `{"attempt", "index", "passed", "reason", "start_t"}`: the attempt number the browser logged (else its 1-based position), 0-based position, whether the browser gate passed it, why it was chosen (`only_attempt`, `last_passed`, `none_passed_used_last`), client time of its first dot event. `null` without calibration dots |
| `attempts` | every attempt: `{"attempt", "index", "passed", "gate_reason", "dots"}` (`gate_reason` is the gate's reason code, `null` if no `calib_gate_result` followed; `dots` = number of dots shown) |
| `viewport` | `{"width", "height"}` in CSS px, the `viewport_change` in force when the used attempt began |
| `dots` | the used attempt's dots: `{"id", "x", "y", "click_t", "valid_frames", "kept_frames"}`; `click_t` is `null` if never clicked; `valid_frames` = training samples of the dot, `kept_frames` = left after outlier removal (0 when failed) |
| `samples` | `{"valid", "kept", "rejected"}`, sums over the dots |
| `cv` | `null` when failed, else `{"linear": {"degree", "alpha", "error_px"}, "quadratic": {...}, "chosen": "linear" or "quadratic", "error_px", "error_norm"}`: the best alpha (grid `calib_alphas`, 1e-3 to 1e3) and the leave-one-dot-out error of a linear and of a degree-2 ridge model (mean over left-out dots of the mean per-frame distance to the dot, px); the one with the lower error is used; `error_norm` is `error_px` over the viewport diagonal. The numbers are those of the second pass, after outlier removal |
| `model` | `null` when failed, else `{"alpha", "degree", "means", "stds", "weights", "intercepts"}`. To predict: `z = (features - means) / stds` (`stds` are population standard deviations); for degree 2 append `z_i * z_j` for `i <= j` (scikit-learn's order); `x = expanded @ weights[0] + intercepts[0]`, `y = expanded @ weights[1] + intercepts[1]` (px). 10 features give 10 (degree 1) or 65 (degree 2) weights per axis, 6 features give 6 or 27 |

Outliers: samples further than `calib_outlier_mad` (2.5) MADs from their dot's median prediction are dropped and the model is chosen and fitted again.

```python
c = json.load(open(f"{d}/calibration_b.json"))
c["status"], c["cv"]["chosen"], c["cv"]["error_norm"], c["feature_columns"]
```

### `derived/gaze_a.parquet` and `gaze_b.parquet` (stages `gaze`, `gaze_b`; PLAN §7, §8.7, §8.8)

One row per webcam frame (the same rows and the same columns for A and B). Not written when the calibration failed (an old file is removed). Prediction: the calibration model applied to every usable frame; smoothing: gaps of invalid samples up to `smooth_max_interp_gap_ms` (150) between valid ones are interpolated linearly, then a Rauch-Tung-Striebel smoother (constant-velocity Kalman model) runs over each stretch of valid and interpolated samples; longer gaps split the stretches.

| column | type | meaning |
|---|---|---|
| `t` | float64 | client time, ms |
| `webcam_pts` | float64 | seconds in `webcam.webm` |
| `video_id` | string, null | the stimulus video on screen at `t`, null otherwise |
| `stim_time` | float64, null | media time of that video in seconds; null outside a video and in a gap (pause, stall, seek) |
| `x_px`, `y_px` | float32, NaN | raw prediction, viewport px; NaN for a frame without usable features (no face, blink); may be outside the viewport |
| `x_sm`, `y_sm` | float32, NaN | smoothed prediction, px; NaN for a sample that is neither valid nor interpolated |
| `x_vid`, `y_vid` | float32, null | `x_sm`, `y_sm` normalised to the video content rectangle (0 to 1, can fall outside); null while no video rectangle is shown or `x_sm` is NaN |
| `valid` | bool | usable features and the prediction lies inside the viewport expanded by `valid_viewport_margin` (0.15) of its size on every side |
| `blink` | bool | the blink flag of `features_a.parquet` (Pipeline B uses A's blinks) |
| `conf` | float32 | heuristic 0 to 1, not a probability: 0 for an invalid sample, 0.5 for an interpolated one, else 1 minus a linear penalty for lying outside the viewport (0 at its edge, 1 at the validity limit) |
| `phase` | string | `calibration` or `validation` from the first to the last dot event of a run of calibration or validation dots (a run ends at the other kind of dot, a gate result, or a video load or end); `playback` while a stimulus video is on screen; `other` otherwise |
| `interpolated` | bool | `x_sm`, `y_sm` were filled by interpolation; `valid` stays false there |
| `x_px_dc`, `y_px_dc` | float32, null | `x_px`, `y_px` with the drift taken out (below) |
| `x_sm_dc`, `y_sm_dc` | float32, null | the same for the smoothed gaze |
| `x_vid_dc`, `y_vid_dc` | float32, null | the drift-corrected smoothed gaze, video-normalised |

The **drift** is the mean offset vector (median prediction minus target, over the points of a validation block with data) at the block's mean click time. The offset at any time is interpolated linearly between the blocks (constant before the first and after the last; one block: constant) and subtracted to give the `_dc` columns. All `_dc` columns are null in every row when no validation block had data (`drift_corrected` false). Use the plain or the `_dc` columns consistently; the analysis takes the `_dc` ones by default.

Schema metadata, key `gaze` (JSON): `version` (1), `pipeline` (`"a"` or `"b"`), `config`, `drift_corrected` (bool), `validation_precision_px` (median over the validation blocks of the RMS sample-to-sample distance, px, or null; it sets the fixation threshold).

```python
g = pd.read_parquet(f"{d}/gaze_b.parquet")
play = g[(g.phase == "playback") & g.valid]
play[["t", "stim_time", "x_vid_dc", "y_vid_dc"]].dropna().head()
```

### `derived/fixations_a.parquet` and `fixations_b.parquet` (PLAN §8.9)

One row per fixation, found by I-DT on the smoothed samples (valid or interpolated) of the playback phase. A run of samples is consecutive frames of the same video in the same viewport. The dispersion limit (x range plus y range of the smoothed px) is `max(fix_dispersion_width_fraction * viewport width, fix_precision_factor * validation_precision_px)` = `max(0.05 * width, 2 * precision)`; the minimum duration is `fix_min_duration_ms` (100). The file exists, possibly with zero rows, whenever the gaze file does.

| column | type | meaning |
|---|---|---|
| `id` | int64 | 0, 1, ... in time order |
| `start_t`, `end_t` | float64 | client time of the first and last sample, ms |
| `duration_ms` | float64 | `end_t - start_t` (so a fixation of n frames lasts about n - 1 frame intervals) |
| `samples` | int64 | number of frames in it |
| `video_id` | string | the stimulus video |
| `stim_start`, `stim_end` | float64, null | media time of the first / last sample, seconds; null in a gap |
| `dispersion_px` | float64 | x range plus y range of its smoothed samples |
| `cx_px`, `cy_px` | float64, null | centroid: mean of `x_sm`, `y_sm`, viewport px |
| `cx_vid`, `cy_vid` | float64, null | mean of `x_vid`, `y_vid` (video-normalised) |
| `cx_px_dc`, `cy_px_dc`, `cx_vid_dc`, `cy_vid_dc` | float64, null | the same from the drift-corrected columns; null if the gaze file has no drift correction |

Schema metadata, key `fixations` (JSON): `version`, `algorithm` (`"I-DT"`), `dispersion_width_fraction`, `precision_factor`, `validation_precision_px` (the value used), `min_duration_ms`, `config`.

```python
fx = pd.read_parquet(f"{d}/fixations_a.parquet")
fx[["start_t", "duration_ms", "stim_start", "cx_vid_dc", "cy_vid_dc"]]
```

### `derived/quality.json` (stage `quality`, PLAN §8.10)

The quality report of Pipeline A and the inclusion decision of the session. The `quality` stage builds it from the other files; the runner then puts the results of the stages (also of `quality` and `plots`, which cannot know their own duration) into `stages`. Pipeline B has its own report, `quality_b.json`.

| key | meaning |
|---|---|
| `version`, `config` | 1; the `PipelineConfig` used |
| `session_flags` | the flags in `raw/meta.json` (those set while the session was uploaded: `interrupted`, `events_invalid`, `calibration_gate_failed` ...); not the flags the worker sets, which are only in the database |
| `is_debug` | the session was run in debug mode |
| `webcam` | `{"frames", "duration_s", "fps_mean", "fps_median", "gaps", "max_gap_ms"}`: effective frame rate of the recording; `gaps` counts steps longer than `quality_frame_gap_ms` (100); rates and `max_gap_ms` are `null` with fewer than 2 frames |
| `face_lost` | `{"all": E, "playback": E or null}` with `E = {"episodes", "total_ms", "longest_ms"}`: stretches without a face of at least `quality_face_lost_min_ms` (500), over the whole recording and inside the playback phase (null when the calibration failed) |
| `events` | counts during the recording: `fullscreen_exits`, `fullscreen_exits_in_playback`, `stalls` (a video stalling after its start), `stalls_at_start`, `page_left` (tab hidden or window blur), `browser_face_lost_episodes` (the browser's own `face_lost` events) |
| `playback` | `null` if the calibration failed, else `{"frames", "valid_frames", "valid_fraction", "blink_fraction", "interpolated_fraction", "per_video": {video_id: {"frames", "valid_fraction"}}}` over the frames with `phase == "playback"` of `gaze_a.parquet`; fractions are `null` without playback frames |
| `face_ratio` | `null` without a face during calibration, else `{"calibration_median", "min_warn", "max_warn", "playback_share_outside"}`: the median `face_ratio` during calibration, the study's two warning multipliers, and the share of playback frames with a face whose `face_ratio` is below `min_warn` or above `max_warn` times that median (`null` if the calibration failed) |
| `browser_gate` | the browser's `calib_gate_result` events: `{"block", "attempt", "passed", "skipped", "mean_error_norm", "reason"}` each |
| `calibration` | `{"status", "failed_reason", "attempt_used", "attempts", "cv", "samples"}`, copied from `calibration_a.json` |
| `validation` | `null` if the calibration failed, else `{"blocks": [...], "precision_px", "drift", "mean_error_norm", "mean_error_corrected_norm"}`. A block (a run of 5 validation dots; `kind` is `before_video`, `between_videos` or `after_last_video`) has `index`, `kind`, `t_ms` (mean click time), `points_with_data`, `mean_error_px`, `mean_error_norm`, `mean_error_corrected_px`, `mean_error_corrected_norm`, `mean_error_deg` (always `null`: degrees need a lab station), `precision_px`, `offset_px` (`{"x", "y"}`, the drift: prediction minus target) and `points` with `id`, `x`, `y`, `click_t`, `frames`, `median_x`, `median_y`, `error_px`, `error_norm`, `error_deg` (null), `error_corrected_px`, `error_corrected_norm`, `precision_px`. Accuracy = distance of the median prediction from the target, precision = RMS of the distance between consecutive frames, both from the valid frames from 700 ms before to 100 ms after the click; `*_norm` = px over the viewport diagonal. `drift` is `{"t_ms", "dx_px", "dy_px"}` (parallel lists, one entry per block with data) or `null`; the top-level `precision_px` and the two `mean_*_norm` are the median / means over the blocks |
| `sync` | `{"lag_ms", "peak", "t_of_pts", "residual_max_ms", "flags"}`, copied from `sync.json` |
| `inclusion` | `{"included", "reasons", "criteria"}`, below |
| `stages` | `{stage: {"seconds", "summary"}}` of the last run of each stage (status and error are in `stages.json`) |

**Inclusion decision** (`decide_inclusion`): the session is included when `reasons` is empty. The thresholds are the study's `inclusion` setting from the config snapshot in `raw/meta.json` (defaults `min_valid_fraction` 0.7, `max_validation_error_norm` 0.12); a value equal to its threshold passes. `criteria` lists the two measured values and the two thresholds: `{"valid_fraction", "min_valid_fraction", "validation_error_norm", "max_validation_error_norm"}`, where `validation_error_norm` is `validation.mean_error_norm` (the uncorrected one).

| reason | when |
|---|---|
| `debug_session` | the session was a debug session (left out by default) |
| `calibration_failed` | `calibration.status` is `failed`; no other reason is given then except `debug_session` |
| `no_playback_data` | no frame has `phase == "playback"` |
| `valid_fraction_low` | the playback `valid_fraction` is below `min_valid_fraction` |
| `no_validation_data` | no validation point had a valid frame |
| `validation_error_high` | `validation_error_norm` is above `max_validation_error_norm` |

**Relation to the database.** `sessions.quality` is a copy of this file taken when a `process_session` run ends (the admin and the analysis's choice of sessions read the decision and `playback.per_video` from there). Once Pipeline B has run, the database column has one more key, `pipeline_b`, that is **not** in `quality.json`; the B job sets it from `quality_b.json` (`summary_of` in `backend/app/pipeline_b.py`): `{"included", "reasons", "model", "calibration_status", "valid_fraction", "validation_error_norm", "stale"}`. `included` and `reasons` are B's own decision (same rule, same thresholds), `model` the network's name, `calibration_status` `ok` or `failed`, `valid_fraction` the playback share, `validation_error_norm` the uncorrected mean error, `stale` whether `pipeline_b_stale.json` existed at that moment. A later run of Pipeline A replaces the rest of the column but keeps `pipeline_b` and refreshes `stale`.

```python
q = json.load(open(f"{d}/quality.json"))
q["inclusion"]["included"], q["inclusion"]["reasons"], q["playback"]["valid_fraction"]
```

### `derived/stages.json` and `stages_b.json` (PLAN §12.2)

What each stage did in its last run, for the admin's stage table (`backend/app/stage_status.py`). `{"version": 1, "stages": {name: {"status", "seconds", "summary", "error"}}}`. Written after every stage, also when one fails, so it shows where a session stopped. `status` is `ok`, `failed` (`error` is `"ExceptionType: message"`, `summary` null) or `skipped` (the summary's `skipped` is a reason as text; today only `"calibration_failed"` for `gaze` / `gaze_b`). A stage that has not run has no entry; a run from a later stage keeps the entries of the earlier ones, and the stages about to run lose their old entry first. The `plots` stages report the plots they could not draw as a mapping under `skipped`, which does not make the stage `skipped`.

| file | stages (in order) |
|---|---|
| `stages.json` | `ingest`, `landmarks`, `features`, `sync`, `calibration`, `gaze`, `quality`, `plots` |
| `stages_b.json` | `features_b`, `calibration_b`, `gaze_b`, `quality_b`, `plots_b` |

`summary` per stage: `ingest` the chunk, byte and line counts and the webcam's frame count, duration, mean fps, codec, size and whether the pts increase; `landmarks` `frames`, `frames_with_face`, `width`, `height`; `features` `frames`, `frames_with_face`, `blinks`; `sync` `lag_ms`, `peak`, `b`, `windows` (their number), `flags`; `calibration` / `calibration_b` `status`, `failed_reason`, `attempt`, `degree`, `cv_error_px`, `samples` (kept); `gaze` / `gaze_b` `frames`, `valid`, `playback_frames`, `valid_playback`, `fixations`, `drift_corrected`, `validation_precision_px`; `quality` / `quality_b` `included`, `reasons`; `plots` / `plots_b` `written` (file names), `skipped` and `errors` (name to reason); `features_b` `frames`, `frames_with_face`, `seconds_per_face` (network time per face, for planning), `model`. A session processed before `stages.json` existed shows the entries stored in `quality.json` under `stages` instead.

### `derived/features_b.parquet` (stage `features_b`, PLAN §8.5)

Pipeline B's features, one row per row of `landmarks.parquet`, made by the gaze network (L2CS-style ONNX model; weights named in `shared/models.json`, not in the repository). B reads `webcam.webm`, `landmarks.parquet` and `features_a.parquet`; its blinks and validity are A's.

| column | type | meaning |
|---|---|---|
| `index`, `pts`, `face` | | as in `features_a.parquet` |
| `gaze_pitch`, `gaze_yaw` | float64, null | the network's gaze angles in radians, as the network gives them (no sign convention applied; the calibration maps them to the screen); null without a face |
| `yaw`, `pitch`, `roll` | float64, null | A's head angles, degrees |
| `face_ratio` | float64, null | A's face scale |
| `blink` | bool | A's blink flag, copied |

**Crop rule** (fixed, so the calibration only has to absorb a constant difference): per frame with a face, the bounding box of all 478 landmarks (in px of the decoded frame) gives a square centred on the box's centre whose side is the box's longer side times `b_crop_scale` (1.2, "expanded 20 %"); it is cut out (black where it leaves the frame), resized to 448 x 448 (bilinear), RGB, scaled to 0 to 1 and normalised with the ImageNet mean and standard deviation. The network gives logits over 90 bins of 4 degrees for yaw and pitch each; the angle is the expected bin position times 4 minus 180 degrees, converted to radians. Faces go through the network `b_batch_size` (8) at a time (the small MobileOne weights run one at a time).

Schema metadata, key `features_b` (JSON): `version` (1), `model` (`{"name", "sha256" of the weights file, "provider"}`; `provider` is the one in use, `cpu` or `cuda`: a CUDA request without a CUDA runtime falls back to the CPU and says `cpu`), `crop_scale`, `input_size` (448), `batch_size`.

```python
fb = pd.read_parquet(f"{d}/features_b.parquet")
import numpy as np; np.degrees(fb[["gaze_pitch", "gaze_yaw"]]).describe()
```

### `derived/quality_b.json` (stage `quality_b`)

Pipeline B's report, same style as `quality.json`. Everything about the recording itself (`webcam`, `face_lost`, `events`, `face_ratio`, `browser_gate`, `sync`, `session_flags`) is only in `quality.json`.

| key | meaning |
|---|---|
| `version`, `pipeline`, `config`, `is_debug` | 1, `"b"`, the `PipelineConfig` used, as in A |
| `features` | the `features_b` schema metadata (`version`, `model`, `crop_scale`, `input_size`, `batch_size`) plus `frames`, `frames_with_face`, `frames_with_angles` (rows with a non-null `gaze_pitch`) |
| `calibration` | `{"status", "failed_reason", "feature_columns", "attempt_used", "cv", "samples"}` from `calibration_b.json` (no `attempts`) |
| `playback`, `validation` | as in `quality.json`, computed from `gaze_b.parquet` and B's predictions; `null` if B's calibration failed |
| `inclusion` | B's own decision, same reasons, rule and thresholds as in `quality.json` (the study's `inclusion`); a failed B calibration gives `calibration_failed` |
| `stages` | `{stage: {"seconds", "summary"}}` of B's stages |

Its headline numbers are copied to `sessions.quality["pipeline_b"]` (see `quality.json`).

### `derived/pipeline_b_stale.json`

Exists only while Pipeline B's files no longer fit Pipeline A's: B reads the landmarks, A's features and the sync, so when A makes any of them again, B's old files describe other inputs. Content: `{"since": ISO 8601 UTC time, "by_stage": "ingest" | "landmarks" | "features" | "sync"}`, the first of those stages that the A run started with.

- **Set** when a `process_session` run (the worker, `cli.py process`, the admin's re-run) starts with any of `ingest`, `landmarks`, `features` or `sync` among its stages, and only if `features_b.parquet` exists. It is set at the start of the run, so it also stays after a failed run. The later stages of A (`calibration` to `plots`) do not set it.
- **Effect:** the B job refuses to start anywhere but at `features_b` (a later stage alone would mix old and new inputs), the admin marks B stale, and `sessions.quality["pipeline_b"]["stale"]` is true. B's old files stay on disk; nothing deletes them.
- **Cleared** by a B run that executes all five stages, i.e. `cli.py process-b <session>` without `--only` / `--from-stage` or with `--from-stage features_b`, or the admin's full run. `--only features_b` does not clear it.

### Debug plots and replay (`derived/debug/`, PLAN §12.2)

PNG pictures drawn by the stages `plots` and `plots_b` with matplotlib; they are for looking at, not for analysis. A plot whose inputs are missing is skipped and its old picture removed; the reasons are in the stage's summary (`skipped`, `errors`), never a failed stage.

| file | content |
|---|---|
| `calibration.png`, `calibration_b.png` | for each calibration attempt the dots (circles) and the model's median prediction for each, joined by lines, and the same for the validation points that followed: how well the attempt's model fitted and carried over |
| `validation.png`, `validation_b.png` | error of every validation block with and without the drift correction, and the drift (offset) over time with its interpolation |
| `gaze.png`, `gaze_b.png` | gaze x and y over time (raw and smoothed) with the phases shaded, invalid stretches grey, interpolated ones purple and the fixations as green bars |
| `sync.png` (A only) | the cross-correlation curve and the drift fit with the windows' residuals |
| `signals.png` (A only) | quality signals over the session: valid fraction, face size, head pose, brightness, webcam frame rate |

The `_b` plots are the same drawings made from B's files, with "(Pipeline B)" in the title; sync and signals do not depend on the pipeline and are not repeated. The admin shows them with the captions in `backend/app/pipeline_names.py`.

**`debug/replay.mp4`** is the session as a film, rendered on request only (`cli.py render-debug <session>` or the admin button; job `render_debug`, `backend/worker/pipeline/replay.py`). **It contains the participant's webcam video** and says so on every frame; it is only served to a logged-in admin. H.264, yuv420p, faststart, no sound, `replay_fps` (30) frames per second along the client clock, `replay_width` (960) px wide plus a 28 px timeline bar. Only the stretches in which a stimulus video was on screen are shown (consecutive videos are joined; a white line marks the join); a paused or stalled video shows its last picture.

- Pipeline A's gaze as circles: raw prediction (orange), smoothed (blue, with a trail of the last third of a second), drift-corrected (green); a green ring around the centre of the fixation the gaze is in, growing with its duration.
- Pipeline B's gaze, when `gaze_b.parquet` exists (whether or not B is stale), as squares: raw (magenta), smoothed (cyan), drift-corrected (yellow) and a slightly larger yellow ring for its fixation; the legend then has a second line. Without B's files the film is the one without it. A point outside the video is a cross on the border.
- The participant's webcam in the bottom right corner (a quarter of the width) with the 49 landmarks of the subset drawn on it (iris points cyan, the others yellow); left out when the recording or its landmarks are missing.
- Text: video id, time since the start of the first shown stretch, video time (or "paused / stalled"), the colour legend, and "gaze invalid" (with "(no face)") when the frame has neither valid nor interpolated gaze (Pipeline A's).
- The bar below: dark where all is fine, grey where A's gaze is invalid, red where no face was found, orange where the video is paused or stalled (the highest applies), a purple tick at every fullscreen exit and a white cursor.

## `analysis/{study_id}/compare/{run_id}/` (PLAN §9.5)

One `compare_pipelines` run (`backend/worker/analysis/compare.py`), started from the admin (`/admin/analysis`) or with `docker compose exec api python cli.py compare <study_id>`. One comparison per study, over every session that is processed, not a debug session, and has a successful calibration in both pipelines and a Pipeline B that is not stale; **A's inclusion decision is not applied** (it would bias the comparison). Other sessions are listed in `run.json` under `excluded`, reasons: `not_processed`, `debug_session`, `calibration_failed_a`, `calibration_failed_b`, `pipeline_b_not_run`, `pipeline_b_stale`, `quality_file_missing`. A run with nothing to compare fails (`status` `failed`, the reason in `error`).

| file | content |
|---|---|
| `run.json` | like the analysis runs': `kind` `compare_pipelines`, `run_id`, `study_id`, `status`, `error`, `started_at`, `finished_at`, `git_hash`, `parameters` (`compare_min_pairs`), `included` and `excluded` (`session_id`, `participant_code`, `reasons`), `outputs`, `results` (`sessions` and `statistics`: the rows of `statistics.csv`) |
| `comparison.csv` | one row per session: `participant_code`, `session_id`, then for each metric `<metric>_a`, `<metric>_b` and `<metric>_diff` (B minus A); empty where a pipeline has no number |
| `statistics.csv` | one row per metric: `n` (sessions with the metric in both pipelines), `median_a`, `median_b`, `median_diff_b_minus_a`, `b_better`, `a_better` (sessions where each is better), `wilcoxon_statistic`, `p_value` |
| `report.md` | a short report: the table of `statistics.csv`, the box plots, who was left out and why |
| `boxplots.png` | a box plot of A and B per metric with a line per session |

Metrics (from `quality.json` and `quality_b.json`, the same keys in both): `accuracy_norm` (mean validation error over the blocks, share of the viewport diagonal), `accuracy_corrected_norm` (the same after the drift correction), `accuracy_px` (mean of the blocks' mean error in px), `precision_px` (the median over the blocks of the RMS step between samples, px), `valid_fraction` (valid gaze share of the playback frames). Lower is better except for `valid_fraction`. The test is `scipy.stats.wilcoxon` (paired, two-sided), per metric, with no correction for several metrics; a p-value is only given from `compare_min_pairs` (6) pairs, and not when every difference is 0.

## Participant code

Two ASCII letters, then birth month and day, for example `AB0315`: `^[A-Z]{2}(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])$`. Input is upper-cased. This is derived from personal data and can collide; see DECISIONS.
