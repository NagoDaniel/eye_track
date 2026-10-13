# Data formats

What the system writes to disk and sends over the wire, as of Milestone 1b. Sections are added as later milestones write more files (PLAN §7). The reference for names and payload shapes is `shared/protocol.json`; `backend/app/schemas.py` and `frontend/src/{config,eventLog}.ts` mirror it and tests compare all three.

## Files under `DATA_ROOT` (default `./data`, `/data` in the containers)

```
videos/{video_id}/video.mp4          stimulus video as uploaded; served by the proxy with range requests
videos/{video_id}/aois.json          optional, written when AOIs are saved in the admin
studies/{study_id}/sessions/{session_id}/
  raw/
    chunks/{stream}/{seq:06d}.bin    chunks exactly as the browser sent them
    events.jsonl                     assembled from the events chunks when the session completes
    meta.json                        session row + config snapshot
```

Every file is written to a temporary name in the same directory and renamed, so a reader never sees a partial file. Raw files are never modified after they are written, except `meta.json` (see below).

Not written yet: `webcam.webm`, `browser_features.jsonl`, `stimulus_frames.jsonl` and everything under `derived/` (Milestones 2 and 3).

## Streams and chunks

Four stream names exist: `webcam`, `features`, `events`, `stimulus_frames`. A session uploads each stream as numbered chunks: `PUT /api/sessions/{id}/chunks/{stream}/{seq}`, raw bytes in the body, header `X-Chunk-SHA256` with the lowercase hex sha256 of the body, bearer upload token. Seqs start at 0 and are contiguous. The server verifies the hash, stores the bytes unchanged and records `(session, stream, seq, size, sha256)` in the `chunks` table. The same seq with the same bytes is accepted again (idempotent); the same seq with other bytes is refused (409).

In Milestone 1b only the `events` stream is used.

## The `events` stream

Each chunk is the **gzip** of newline-terminated JSON lines. The browser starts a new chunk every `recording.chunk_ms` (default 2000 ms) and sends none for an empty interval. The sha256 is over the compressed bytes. `raw/events.jsonl` is the gunzipped chunks concatenated in seq order (`backend/app/events.py`, also used by the later ingest stage).

One line:

```json
{"t": 1234.5, "type": "calib_dot_show", "data": {"phase": "calibration", "id": 0, "x": 640, "y": 360}}
```

- `t` is `performance.now()` in milliseconds on the participant's clock (PLAN §5.3); events are written in the order they happened.
- `type` is one of the names in `shared/protocol.json` (`event_types`). The server refuses to complete a session that has an unknown type or an invalid line.
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

Names that exist but are not written yet (Milestone 2): `calib_gate_result`, `face_lost`, `face_regained`, `recorder_start`, `recorder_dataavailable`, `recorder_stop`, `debug_skip`.

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
- `flags` contains `interrupted` when the participant reloaded the page mid-session and finished the upload from a later page load.

## Session status

`created` (session made) → `consented` (consent recorded) → `recording` (first chunk accepted) → `uploaded` (`/complete` found no missing chunk). `processing`, `processed`, `failed` and `withdrawn` exist in the database but nothing sets them yet. Completing a session does not enqueue a job until Milestone 3.

## Participant code

Two ASCII letters, then birth month and day, for example `AB0315`: `^[A-Z]{2}(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])$`. Input is upper-cased. This is derived from personal data and can collide; see DECISIONS.
