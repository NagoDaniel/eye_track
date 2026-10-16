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
