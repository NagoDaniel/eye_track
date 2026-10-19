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
    stages.json                      per stage: status (ok, skipped, failed), seconds, summary, error; written after every stage
    debug/*.png                      debug plots (below)
    debug/replay.mp4                 the debug replay video, only when rendered on request (below)
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

`created` (session made) → `consented` (consent recorded) → `recording` (first chunk accepted) → `uploaded` (`/complete` found no missing chunk and queued the `process_session` job) → `processing` (the worker runs the stages) → `processed` (the quality report exists) or `failed` (a stage raised; the job's log names the stage). `withdrawn` exists in the database but nothing sets it yet.

`/complete` queues the job once per session (a repeated `/complete` does not queue another) and not for a session without any chunk (flag `empty`). Job kinds: `process_session` (the stages, above) and `render_debug` (the replay video; it does not change the session's status). A session can have only one open job of either kind at a time. A processed or failed session can be processed again from any stage: a `process_session` job with `from_stage` or `only` in its args (the CLI and the admin page do this). A worker that dies mid-job leaves the session `processing`; the next worker start puts the job back in the queue and the session back to `uploaded`, or fails both after `JOB_MAX_ATTEMPTS` (DECISIONS milestone 3 item 14).

The worker sets these session flags (and removes them when a later run no longer finds the problem), next to the ones of the participant app: `sync_short`, `sync_weak_peak`, `sync_lag_at_limit`, `sync_drift_high`, `sync_residual_high`, `sync_drift_unknown`, `sync_coarse_only` (see `sync.json`) and `calibration_failed` (see `calibration_a.json`). `sessions.quality` holds `quality.json`.

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

`rect` coords are `[x0, y0, x1, y1]` with x1 > x0, y1 > y0; `polygon` has at least 3 `[x, y]` points (concave allowed). Both lists default to empty; ids are unique within their list; `end` is after `start`. An AOI is active from `start` up to but excluding `end`. A point on the border is inside. The hit tolerance grows the shape by a distance (round corners).

## `analysis/{study_id}/{video_id}/{run_id}/run.json` (PLAN §9)

One `analyze_video` run (`backend/worker/analysis/run.py`). `run_id` is `YYYYMMDDTHHMMSSZ-xxxxxx` (UTC start time, random tail); a run directory is never reused. `run.json` is written when the run starts (`status` `running`) and again when it ends (`done`, or `failed` with `error`), so a run that died shows as `running`.

| key | meaning |
|---|---|
| `version`, `kind` | 1, `analyze_video` |
| `run_id`, `study_id`, `video_id` | |
| `status`, `error`, `started_at`, `finished_at` | ISO 8601 UTC |
| `git_hash` | `git describe --always --dirty` of the build (`GIT_SHA` build argument); `null` when the image was built without it |
| `parameters` | every value of `AnalysisConfig` that was used (pipeline, `gaze_columns` `plain` / `drift_corrected`, heatmap window, sigma floor, grid, segment length, AOI tolerance k, synchrony window) |
| `included` | `{session_id, participant_code, ...}` per session analysed (later stages add their numbers) |
| `excluded` | `{session_id, participant_code, reasons}` per session left out. Reasons: `not_processed`, `video_not_watched`, the reasons of the session's inclusion decision (`quality.json`), `gaze_file_missing`, `superseded` (an older session of a participant who has a later included one) |
| `outputs` | files written, relative to the run directory |
| `results` | numbers the stages report |

Sessions that never finished uploading and withdrawn ones are not listed. One participant code counts once: its latest included session.

### `heatmap.mp4` and `density.npz` (PLAN §9.1)

Written by `backend/worker/analysis/heatmap_video.py` into the run directory. `heatmap.mp4` is the stimulus video (same frames, size cropped to even sides, frame rate) with the group density drawn on it: H.264, yuv420p, faststart; hotter is warmer (Turbo colour map) and more opaque, up to `heatmap_alpha_max`. One colour scale serves the whole video: the `heatmap_scale_percentile` (99) of the non-zero densities of all frames is fully hot. With `heatmap_dots` on, one white dot per participant is drawn at the median of their samples in the window.

For stimulus frame `k` (media time `s = k / fps`) the density uses the valid samples of every included participant with `stim_time` in `[s - heatmap_window_s, s + heatmap_window_s]` (video-normalized coordinates, from the plain or the drift-corrected columns as the run says). A participant's density is the mean of Gaussians at their samples, so each participant weighs the same; the group density is their sum. The Gaussian is isotropic in video-normalized units with `sigma` = the median over included sessions of the mean validation accuracy in px divided by the width of the video's content rectangle on that participant's screen, at least `sigma_min` (DECISIONS 2026-10-19 item 3).

`density.npz`: `density` float32 `(frames, 36, 64)` (row = y from the top, column = x; cell centres at `(i + 0.5) / 64`, `(j + 0.5) / 36` of the video), and scalars `fps`, `sigma`, `scale` (the density drawn fully hot), `window_s`.

### `static/NNN_<id>.png` (PLAN §9.2)

Written by `backend/worker/analysis/heatmap_static.py`, one picture per time segment, numbered in segment order (the id is made file-name safe). The segments are the `segments` of the video's `aois.json`, or consecutive windows of `segment_default_s` (10) seconds over the whole video, the last one shorter; a segment is cut at the end of the video and one that starts after it is dropped. The density is the one of the dynamic heatmap (same sigma, equal weight per participant) over all samples of the participant in the segment, drawn over the video frame at the segment's middle. Each picture has its own colour scale (the 99th percentile of its non-zero densities).

### `aoi_metrics_long.csv` and `aoi_metrics_wide.csv` (PLAN §9.3)

Only written when the video has AOIs. Code: `backend/worker/analysis/aoi_metrics.py`. Per included participant and AOI, over the time the AOI exists (`start` to `end`, cut at the end of the video); coordinates are video-normalized and the shape is grown by `aoi_tolerance_k` times the participant's own validation accuracy (in video units: px over the content rectangle's width; no growth when the accuracy is unknown). The long file has the columns `participant_code, aoi_id, metric, value`; the wide file has one row per participant and AOI and one column per metric. An empty value means "not defined" (no fixation hit the AOI).

| metric | meaning |
|---|---|
| `dwell_fixation_s` | time of the fixations whose centroid is in the shape; a fixation that starts before or ends after the AOI counts for the part inside the active time |
| `dwell_sample_s` | time of the valid samples in the shape; a sample stands for the time to the next webcam frame, at most `sample_max_dt_ms` (100) |
| `share_active` | `dwell_sample_s` over the AOI's active time (empty when the AOI starts after the video ends) |
| `time_to_first_fixation_s` | from the AOI's start to the start of the first hitting fixation (0 when it was already there); empty when none |
| `fixation_count` | number of hitting fixations |
| `revisits` | entries minus one (at least 0); an entry is a run of consecutive hitting fixations among the fixations in the active time |

## Participant code

Two ASCII letters, then birth month and day, for example `AB0315`: `^[A-Z]{2}(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])$`. Input is upper-cased. This is derived from personal data and can collide; see DECISIONS.
