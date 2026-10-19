# Progress

Maintained by the implementing agent. Update at the end of every task and before ending a session. Keep it short: this is the hand-over note for the next session.

## Current milestone
**4 — Analysis** (PLAN §14): in progress on branch `m4-analysis`, task list below. Previous milestone: **3 — Pipeline A** (PLAN §14): accepted by Nágó Dániel on 2026-10-18, merged via PR from branch `m3-pipeline-a`; task list below, scope choices in DECISIONS (2026-10-16, "Milestone 3 scope and open choices"). Milestone 2 (Recording and sync data) was accepted by Nágó Dániel and merged into `main` on 2026-10-16; its task list, results, the first real-run fixes and the review are in git history (last commit before the merge: `ca0bbb3`) and in DECISIONS (entries of 2026-10-15 and 2026-10-16).

## Milestone status
| Milestone | Status | Branch | Notes |
|---|---|---|---|
| 1a Infrastructure | done (accepted and merged 2026-10-12) | m1a-infrastructure | task record and review are in git history (`41e299d`) |
| 1b Skeleton app | done (accepted and merged 2026-10-15) | m1b-skeleton-app | PR #2; task record and review fixes in git history (`745c48c`) |
| 2 Recording and sync data | done (accepted and merged 2026-10-16) | m2-recording | reviewed by the plan-reviewer, fixes done except the items under "Open problems"; `make e2e` produces complete raw data (webcam WebM, features, stimulus frames, events) |
| 3 Pipeline A | done (accepted 2026-10-18; H1 passed, review fixes done) | m3-pipeline-a | `make lint`, `make test` (658 backend, 474 frontend tests) and `make e2e` pass; the e2e now runs the whole pipeline in the Docker image, the admin inspector and the replay |
| 4 Analysis | in progress | m4-analysis | task list approved 2026-10-19 |
| 5 Pipeline B and comparison | not started | | |
| 6 Hardening for pilot | not started | | |

## Approved task list (Milestone 4)
Approved by Nágó Dániel on 2026-10-19 (choices in DECISIONS 2026-10-19, "Milestone 4 scope and open choices"). Status: `todo` / `doing` / `done`. Each task updates `docs/DATA_FORMATS.md` for what it writes and ends with its tests shown and a commit `M4: <what> (PLAN §x.y)`. The Milestone 3 task list is in git history (`6336f6a`).

| # | Task | PLAN | Tests | Human |
|---|---|---|---|---|
| T1 | **done** (2026-10-19). AOI schema (pydantic, one definition) for `segments` and `aois` (`rect`, `polygon`), checked when the admin saves; hit test with tolerance. `app/aois.py`, `app/videos.py` | §9.3, §4 | AOI hit tests (§13): rect and polygon edges, concave polygon, tolerance growth, active-time limits; admin save rejects bad AOIs | no |
| T2 | **done** (2026-10-19). `AnalysisConfig` with every §9 number; git hash recorded at image build. `worker/analysis/config.py`, `Dockerfile`, `Makefile`, `scripts/setup.sh` | §1, §9 | defaults equal the plan; hash present in the image | no |
| T3 | **done** (2026-10-19). Run skeleton: session selection, `run_id`, run directory, `run.json`, loaders for samples and fixations on the video. `worker/analysis/run.py`, `select.py`, `load.py` | §9 | real Postgres: included, excluded, superseded and not-watched cases give the right reasons; `run.json` schema | no |
| T4 | todo. Density core: sum of Gaussians, sigma from median validation accuracy (floor 0.03), equal weight per participant, 64x36 grid. `worker/analysis/density.py` | §9.1 | synthetic: known Gaussian location, 10:1 sample counts get equal mass, sigma floor | no |
| T5 | todo. Dynamic heatmap video and `density.npz`. `worker/analysis/heatmap_video.py` | §9.1 | real PyAV and ffmpeg on `moving_dot.mp4` with gaze on the known path: frame count, fps, size, peak near the dot, npz shape | yes (look at a heatmap) |
| T6 | todo. Static heatmaps per segment. `worker/analysis/heatmap_static.py` | §9.2 | one valid PNG per segment; default 10 s windows cover the video | no |
| T7 | todo. AOI metrics, long and wide CSV, tolerance k. `worker/analysis/aoi_metrics.py` | §9.3 | hand-computed values incl. straddling fixations, never-hit AOI, k > 0 | no |
| T8 | todo. Attentional synchrony (dispersion, NSS), CSV and PNG. `worker/analysis/synchrony.py` | §9.4 | identical participants give 0; known spread; NSS high at peak, negative for outlier; fewer than 2 participants gives NaN | no |
| T9 | todo. `analyze_video` handler and CLI `analyze`. `worker/handlers.py`, `worker/run.py`, `cli.py` | §9, §12.3 | real fixture: `session_e2e` processed for real, copied as 3 participants with known gaze offsets, full run; handler on real Postgres; failing run keeps finished outputs | no |
| T10 | todo. ZIP export of a run. `app/api/admin_analysis.py` | §6 | ZIP holds every run file; 401 anonymous, 200 admin | no |
| T11 | todo. Admin analysis pages (start with CSRF, run list, run detail) and the participant page results. templates, `participants_view.py` | §6, §9 | API tests for auth, CSRF, listing; page tests | no |
| T12 | todo. E2E: start an analysis in admin, wait for the job, download the ZIP. `e2e/tests/` | §13 | Playwright | no |
| T13 | todo. `DATA_FORMATS.md` for `aois.json`, `run.json` and every output; researcher notes on writing AOIs | §7, §9.3 | docs | no |

**Done when (PLAN §14 gives none; agreed):** `analyze_video` on at least 2 real processed sessions writes every §9.1 to §9.4 output and `run.json`; admin can start a run, view it and download the ZIP (T12); the human sees the heat follow the dot (**H2:** two or three moving-dot sessions in one study, a run in admin, look at the heatmap video and the synchrony plot).

## Open problems / questions for the human
- One `make e2e` run in five (2026-10-18, while T13 was tested) failed its first browser test: the participant page stayed on an empty screen for the whole 100 s of `runSession` (page snapshot: an empty `main` with one clickable element), although the server logged a normal session. The same code passed on the three runs before and the one after; no cause found. If it recurs, keep `e2e/test-results/*/trace.zip` (`npx playwright show-trace`).
- From the M2 review, not fixed (major 2, decide before real participants; Milestone 6 at the latest): `/complete` inflates and validates all chunks of a stream in the API process; each chunk is capped (`MAX_EVENTS_CHUNK_INFLATED_MB`) but there is no total per stream, so someone with a study code could ask for GBs of memory and a long validation within the 2 GiB per-session cap. Proposed fix: a total inflated cap per stream, or validating in the M3 ingest worker. Decide before real participants (Milestone 6 at the latest).
- `npm audit` reports 3 findings (2 critical) in `tinypool`, a vitest 3 dependency (dev only, never shipped). Fix is a breaking upgrade to vitest 4; not done. Decide whether to upgrade (suggest doing it in a small separate task).
- Alembic autogenerate quirk, cause NOT verified. With `Enum(native_enum=False, create_constraint=True)` (`str_enum` in `backend/app/models.py`), autogenerate (alembic 1.20, SQLAlchemy 2.1.4) wrote each enum CHECK twice as explicit `sa.CheckConstraint(...)` lines on top of the `sa.Enum(..., create_constraint=True)` column, which already creates it. `create_table` then failed with `check constraint "job_kind" already exists`. Workaround used in migration `0001`: delete all explicit `sa.CheckConstraint` lines (the Enum column creates them; `test_migrations_match_models` and the CHECK test confirm the result). Table metadata holds each constraint once, so the duplication happens at render time. Not yet isolated in a minimal repro; do that if it recurs in a later migration (or switch to `create_constraint=False` plus an explicit `CheckConstraint` in `__table_args__`).
- Participant codes (initials + birth month + day, human's choice) are derived from personal data and can collide; a collision matters at withdrawal (M6), which deletes all data for a code. Revisit before real data collection.

## H1 result (real webcam, moving-dot video, 2026-10-18)
Two debug sessions by Nágó Dániel, screen 1710 px wide, `cli.py moving-dot-report`: both **PASS** (median error at most 0.15 of the diagonal, at least 80 % valid).

| | A, first run (glasses), `080ebab8` | B, no glasses, `fb226e78` |
|---|---|---|
| median error, whole video | 197 px (0.098) | 92 px (0.046) |
| median error in pauses / movements | 188 / 232 px | 76 / 176 px |
| valid frames in playback | 98 % | 98 % |
| calibration cross-validation error | 177 px | 116 px |
| validation jitter (frame to frame) | 93 px | 31 px |
| gaze lag behind the dot | +160 ms | +160 ms |

Sync on the real recordings (`derived/sync.json`): A (153 s, the first real use of the windowed drift fit) has 3 windows, all used, lags -32.3 / -33.9 / -32.9 ms, correlation 0.997 in each, residual at most 1.3 ms, slope error 5.7e-6, no flags; B (73 s) is `sync_short` with lag -53.5 ms, correlation 0.999.

Findings (details in DECISIONS "Milestone 3 pilot result"): vertical gaze is the weak axis; A is worse but the glasses cannot be told apart from posture and fatigue with two sessions; the +160 ms lag is the same in both; the mean-offset drift correction does not help on this test; a smaller feature set is worse than the 10 features; the linear calibration model beats the quadratic one. The browser gate and the server's validation agree. Tuning is deferred (human, 2026-10-18).

## Review of Milestone 3 (plan-reviewer, 2026-10-18): fixes
Approved by Nágó Dániel on 2026-10-18 and **done**: R1, R2, R3 (`docs/evidence/m3-h1/`), R4, R6, R8. **Dropped by the human:** R5, R7, R9 and the minor items below (replay loaded on every page view, participant-video access not logged, M6 retention follow-up).
Verdict: **READY AFTER FIXES**. No blocker. `make lint`, `make test` (650 backend, 474 frontend) and `make e2e` all passed in the reviewer's own run. The full report is in the conversation of 2026-10-18; these are the tasks it asks for. Approve, change or drop them by number.

| # | Severity | Task | Test |
|---|---|---|---|
| R1 | major | `import-session --force` deletes the existing session before the archive is verified (`app/session_archive.py:163-179`). Extract and verify into a temporary directory first, swap only when everything is fine | `--force` with a corrupt archive leaves the existing session's files and rows unchanged |
| R2 | major | The stage table loses its data when a stage fails: stage results are only in `quality.json`, written after the loop (`worker/pipeline/runner.py:193-199`). Write the per-stage status (ok, skipped, failed with the error) after every stage and on failure to its own file (`derived/stages.json`); the admin stage table reads it and shows status | a session that fails at `landmarks` shows ingest as done with its duration and landmarks as failed; admin page test |
| R3 | major | Evidence for "done when" lives only in typed numbers. Save the `moving-dot-report --json` output of both H1 sessions and the sync summary as `docs/evidence/m3-h1/` (aggregate numbers only, no gaze rows, no video; needs the human's OK because they are the human's own sessions in a public repo), and say in the PR text that the agreed criterion (0.15) is looser than the gate (0.10) and the inclusion threshold (0.12) | files exist and parse |
| R4 | minor | `worker/pipeline/moving_dot.py:75` hard-codes 0.15; use `config.dot_max_median_error_norm` | the within-threshold share follows the config |
| R5 | minor, privacy | The replay contains the participant's webcam picture: (a) `export-session --no-raw-video` must leave `debug/replay.mp4` out (`_is_raw_video`), (b) the session page must not load it on every view (`preload="none"`), (c) the access log gets the admin's username, (d) note for M6: retention and withdrawal must delete the replay too | export test; page test; log test |
| R6 | minor | DECISIONS entry: calibration training uses frames with a face, no blink and finite features (not the plan's `valid`, which depends on the model being fitted) | doc only |
| R7 | minor | Import hardening: `ArchiveError` instead of `ValueError`/`KeyError` for an unknown stream or a missing `participant_code`; a size limit on archive members | tests for each |
| R8 | minor | `cli.py process` refuses when a job for the session is open (same rule as the admin button) | CLI test |
| R9 | minor | `replay.encode`: no `assert` for control flow; a broken ffmpeg pipe reports ffmpeg's stderr | test with a failing ffmpeg command |

Not proposed as tasks (accepted or later): plot failures are recorded in the stage summary but do not flag the session (documented, DECISIONS T13); the participant results page shows the inclusion line and a link, not the whole report (T14); no test kills a worker in the middle of a real pipeline run (the two halves are tested); the real-fixture lag bound of 150 ms is loose; the flaky first e2e test (see Open problems).

## Your checklist for Milestone 3
Everything below needs a real webcam, real lighting, or your decision. The code and tests are done.

**A. Put the new code on a machine you can use a webcam on**
1. Local: `make dev` (needs `.env`: `./scripts/setup.sh --env-only` makes one), open http://localhost:8080 (`localhost` counts as a secure context, so the camera works without TLS). Then `docker compose -f docker-compose.yml -f compose.dev.yml exec api python cli.py create-admin --username <you>`. The migration `0002` (job kind `render_debug`) runs by itself.
2. Or the Hetzner VPS (stopped, still on the old 1a build): start it, `cd /opt/eyetrack && git pull && docker compose up -d --build`. Do this only if you want HTTPS and a second device.
3. Log in at `/admin`.

**B. The moving-dot run (H1)**
4. Admin, Videos: upload `e2e/fixtures/moving_dot.mp4` (title "Moving dot").
5. Admin, Studies: new study, playlist = only that video, everything else default. Copy the participant link.
6. In Chrome (not Safari), open the link in a quiet room: face lit from the front (no window behind you), about an arm's length from the screen, head still, glasses as you normally wear them. Enter a participant code (e.g. `AB0315`), give consent, go through the device check, and click each calibration dot when it has shrunk. During the video, **follow the dot with your eyes only**; do not move your head.
7. Do a second run if the first one goes badly (a blink at the wrong moment, a dot missed): note what went wrong, it is data about the pipeline too.

**C. Look at what the pipeline made (admin, Sessions, your session; about 1 to 2 minutes after the upload)**
8. Status becomes `processed`. If it says `failed`, open the session page: the "Why it failed" box has the reason; send it to me.
9. Read the quality report: included or not and why, valid gaze, validation error with and without drift correction, calibration, sync, face lost.
10. Open the five plots. Say what is unclear or missing.
11. Press "Render the debug replay" (takes about as long as the video), then play it: do the dots (orange raw, blue smoothed, green drift-corrected) follow the moving dot? Is the webcam corner with the landmarks right? Does the timeline bar show what happened? Also open "Show the webcam recording" once to check that page.
12. In a terminal: `docker compose exec api python cli.py moving-dot-report <session id> --plot /data/dot.png` (the file ends up in your `DATA_DIR`). Exit status 0 = pass (median error at most 0.15 of the screen diagonal and at least 80 % valid frames), 2 = fail.

**D. What to send me**
13. The text of the `moving-dot-report` output (or `--json`), `dot.png`, and your answers to steps 9 to 11 (screenshots help). If it fails, say what you saw in the replay: gaze lagging, constant offset, jumping, or lost face.

**E. Your decisions before the milestone is closed**
14. Read `docs/DECISIONS.md` (entries of 2026-10-16 "Milestone 3 scope" and 2026-10-18, T1 to T15) and say which choices you object to; none is blocking by itself. The ones most worth a look: inclusion rules (T10), fixations on valid or interpolated samples (T9, you already approved), sessions without a webcam end `failed` (T11), what the participant video may be used for in the admin (T15).
15. Tell me when to run `/milestone-review 3` (the plan-reviewer checks the diff against the plan), and after the fixes when to push `m3-pipeline-a`, open the PR and merge. Pushing and the PR are outward-facing, so I wait for your word. CI downloads the MediaPipe model during the tests, so it needs network.
16. Threshold tuning (blink openness, smoother noise, quality thresholds, gate limits) is deferred until there is pilot data; the numbers from step 12 are the first real data for that. Say if you want a tuning task after the review.

**F. Small things you may want to deal with**
17. The untracked `face_test.mov` in the repo root: delete or move it (the 12 s clip `e2e/fixtures/face.mp4` is committed, your face, public repo; say if you want that removed or replaced).
18. The Hetzner VPS is still on the old build: update or delete it.
19. Postponed since earlier: the fresh-VM run of PLAN §11.6, the M2 review's major 2 (total inflated size per stream in `/complete`, before real participants), the `npm audit` vitest upgrade, participant-code collisions (before real data).
20. One `make e2e` run in five failed its first browser test once (empty page for 100 s, no cause found, see "Open problems"). If it happens to you, keep `e2e/test-results/*/trace.zip` and tell me.

## Next step
Milestone 3 is merged. Run `/milestone-start 4` (Analysis: heatmaps, AOI metrics, synchrony). Precision tuning of the gaze models is deferred (DECISIONS 2026-10-18). For Milestone 4: the per-session outputs to build on are `derived/gaze_a.parquet` (smoothed and drift-corrected columns, `x_vid`/`y_vid`, `stim_time`, `valid`, `phase`), `derived/fixations_a.parquet`, `derived/quality.json` (the inclusion decision per session) and `sessions.quality`; `PipelineConfig` holds every threshold; the participant results page and the admin session page are where per-participant results go (DECISIONS 2026-10-14).
