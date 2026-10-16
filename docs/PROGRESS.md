# Progress

Maintained by the implementing agent. Update at the end of every task and before ending a session. Keep it short: this is the hand-over note for the next session.

## Current milestone
**3 — Pipeline A** (PLAN §14): in progress on branch `m3-pipeline-a`; task list below, scope choices in DECISIONS (2026-10-16, "Milestone 3 scope and open choices"). Milestone 2 (Recording and sync data) was accepted by Nágó Dániel and merged into `main` on 2026-10-16; its task list, results, the first real-run fixes and the review are in git history (last commit before the merge: `ca0bbb3`) and in DECISIONS (entries of 2026-10-15 and 2026-10-16).

## Milestone status
| Milestone | Status | Branch | Notes |
|---|---|---|---|
| 1a Infrastructure | done (accepted and merged 2026-10-12) | m1a-infrastructure | task record and review are in git history (`41e299d`) |
| 1b Skeleton app | done (accepted and merged 2026-10-15) | m1b-skeleton-app | PR #2; task record and review fixes in git history (`745c48c`) |
| 2 Recording and sync data | done (accepted and merged 2026-10-16) | m2-recording | reviewed by the plan-reviewer, fixes done except the items under "Open problems"; `make e2e` produces complete raw data (webcam WebM, features, stimulus frames, events) |
| 3 Pipeline A | in progress | m3-pipeline-a | carries over R2 (stale `running` jobs) from the 1a review; the debug replay video (§12.2) and offline gaze estimation are here |
| 4 Analysis | not started | | |
| 5 Pipeline B and comparison | not started | | |
| 6 Hardening for pilot | not started | | |

## Approved task list (Milestone 3)
Approved by Nágó Dániel on 2026-10-16. Status: `todo` / `doing` / `done`. Each task updates `docs/DATA_FORMATS.md` for what it writes and ends with its tests shown and a commit `M3: <what> (PLAN §x.y)`.

| # | Task | PLAN | Tests (real data where the stage needs it) | Human |
|---|---|---|---|---|
| T1 | todo. Stale `running` jobs: requeue or fail at worker start (DECISIONS item 14); tests for `worker.run.main()` (SIGTERM, concurrency guard). `app/jobs.py`, `worker/run.py` | DECISIONS 2026-10-12 | real Postgres: kill a worker mid-job, restart, check job and session state; SIGTERM test | no |
| T2 | todo. Dependencies (numpy, scipy, pandas, pyarrow, matplotlib, scikit-learn); session fixture capture script and fixture (item 1); ingest: sha256 and completeness check, webcam concatenation, ffmpeg remux, per-frame pts (PyAV), JSONL streams. `worker/pipeline/ingest.py`, `scripts/make_session_fixture.py`, `shared/fixtures/session_e2e/` | §8.1, §7 | real ffmpeg and PyAV on the fixture: remux seekable with duration, frame count, missing or corrupt chunk fails loudly | no |
| T3 | todo. Server landmarks, full-res MediaPipe VIDEO mode, `landmarks.parquet` (item 2). `worker/pipeline/landmarks.py` | §8.3 | real MediaPipe on the first frames of the fixture: 478 float32 landmarks, one face per frame | no |
| T4 | todo. Features A table and blink rule; `features_a.parquet`. `features_a.py` | §8.4 | `shared/fixtures/face_frames.json` equals `frame_features`; blinks found on the fixture | no |
| T5 | todo. Sync (coarse offset, +-1000 ms cross-correlation at 100 Hz, 60 s windows, Huber drift, `sync.json`, flags) and stimulus-time map. `sync.py`, `stimulus_map.py` | §8.2 | synthetic known offset and drift recovered; mapping across pause and stall gaps; real fixture: browser features vs server landmarks give lag near 0 | no |
| T6 | todo. Calibration fit: per-dot training frames and attempt choice (item 3), standardize, degree 2, ridge, leave-one-dot-out CV over alpha, plain linear model, MAD rejection, `failed` rule; `calibration_a.json`. `calibration.py` | §8.6, §5.6 | synthetic known mapping recovered; ridge equals `ridge_fit.json`; runs end to end on the fixture (plumbing only) | no |
| T7 | todo. Prediction, validity (viewport + 15 %), interpolation, RTS Kalman smoother. `predict.py`, `smoothing.py` | §8.7 | 300 px step settles in about 60 ms (item 6); segments split at gaps over 150 ms; interpolated samples stay invalid | no |
| T8 | todo. Validation accuracy and precision per block, drift offsets, `drift_corrected` columns. `validation.py` | §8.8 | synthetic constant and linear drift removed; per-point errors against hand-computed values | no |
| T9 | todo. I-DT fixations, `fixations_a.parquet`, assemble `gaze_a.parquet` with video coordinates and `conf` (item 5). `fixations.py`, `gaze.py` | §8.9, §7 | synthetic scanpath with known fixations; schema test on the fixture; `x_vid` against the logged `video_rect` | no |
| T10 | todo. Quality report `quality.json` and inclusion decision. `quality.py` | §8.10, §4.1 | fixture report has every field; inclusion pass and fail with reasons | no |
| T11 | todo. Stage runner, `process_session` handler, enqueue at `/complete`, statuses (item 15), result into `Session.quality`; Playwright e2e processes the session and checks the derived files and schemas. `worker/pipeline/runner.py`, `worker/run.py`, `api/participant.py`, `e2e/tests/dummy-session.spec.ts` | §8, §6, §13 | stage re-run gives identical output; failing stage marks job failed and keeps earlier outputs; e2e | no |
| T12 | todo. CLI: `process <id> --from-stage/--only/--keep-intermediates`, `export-session`, `import-session`, `moving-dot-report <id>`. `cli.py` | §12.3, §11.4 | export, delete, import, process round trip with the same hashes; moving-dot report on synthetic gaze with known errors | no |
| T13 | todo. Debug plots into `derived/debug/` (calibration per attempt, validation over time, sync, quality signals, gaze over time, moving-dot path vs gaze). `worker/pipeline/plots.py` | §12.2 | the fixture produces every PNG, valid and non-empty | yes (look at plots on a real session) |
| T14 | todo. Admin session inspector: quality report, plots, stage table with "re-run from this stage" (CSRF), quality summary on the participant page. templates, `api/admin_sessions.py`, `sessions_view.py` | §12.2 | Playwright opens the processed session and finds report, plots and stage table; API tests for auth and CSRF | no |
| T15 | todo. Debug replay video (`render-debug`, job kind `render_debug` with migration, admin trigger) and admin-only webcam playback with Range support. `worker/pipeline/replay.py`, `cli.py`, admin routes | §12.2, DECISIONS 2026-10-14 | real ffmpeg render of the fixture (frame count, size); webcam endpoint 401 anonymous, 206 for an admin | yes (look at the replay) |

**Done when (PLAN §14) and where it is shown:**
- The quality report renders in admin: T10 builds it, T11 produces it for a real session, T14 renders it and its e2e test asserts it.
- The moving-dot fixture shows plausible tracking on a real webcam: T9 and T11 produce the gaze, T12 measures it (criterion in DECISIONS item 9), T13 and T15 show it. **H1 (human, after T15):** make a study with `moving_dot.mp4` as the only video, run it once on a real webcam, run `cli.py moving-dot-report <session_id>`, open the debug replay, report the numbers and whether the dot is followed.

## Open problems / questions for the human
- From the M2 review, not fixed (major 2, decide before real participants; Milestone 6 at the latest): `/complete` inflates and validates all chunks of a stream in the API process; each chunk is capped (`MAX_EVENTS_CHUNK_INFLATED_MB`) but there is no total per stream, so someone with a study code could ask for GBs of memory and a long validation within the 2 GiB per-session cap. Proposed fix: a total inflated cap per stream, or validating in the M3 ingest worker. Decide before real participants (Milestone 6 at the latest).
- `npm audit` reports 3 findings (2 critical) in `tinypool`, a vitest 3 dependency (dev only, never shipped). Fix is a breaking upgrade to vitest 4; not done. Decide whether to upgrade (suggest doing it in a small separate task).
- Alembic autogenerate quirk, cause NOT verified. With `Enum(native_enum=False, create_constraint=True)` (`str_enum` in `backend/app/models.py`), autogenerate (alembic 1.20, SQLAlchemy 2.1.4) wrote each enum CHECK twice as explicit `sa.CheckConstraint(...)` lines on top of the `sa.Enum(..., create_constraint=True)` column, which already creates it. `create_table` then failed with `check constraint "job_kind" already exists`. Workaround used in migration `0001`: delete all explicit `sa.CheckConstraint` lines (the Enum column creates them; `test_migrations_match_models` and the CHECK test confirm the result). Table metadata holds each constraint once, so the duplication happens at render time. Not yet isolated in a minimal repro; do that if it recurs in a later migration (or switch to `create_constraint=False` plus an explicit `CheckConstraint` in `__table_args__`).
- Participant codes (initials + birth month + day, human's choice) are derived from personal data and can collide; a collision matters at withdrawal (M6), which deletes all data for a code. Revisit before real data collection.
- From the 1a review: stale `running` jobs (R2) must be solved in M3 (DECISIONS).

## Needs human testing
- Fresh-VM run of PLAN §11.6 (postponed by the human).
- The Hetzner VPS is stopped (human, 2026-10-15). It still has the pre-review 1a build; update it (`cd /opt/eyetrack && git pull && docker compose up -d --build`) before the next use, or delete it if it is no longer needed.
- Face clip: committed as `e2e/fixtures/face.mp4` (12 s, your face, public repo) and 45 landmark frames in `shared/fixtures/`. Say if you want it removed or replaced; the untracked original `face_test.mov` in the repo root can be deleted or moved by you.
- Fine-tuning of the quality thresholds, blink openness, gate limits and the browser model's features is deferred to after M3, with the offline pipeline and pilot data (human, 2026-10-16). The starting values and the evidence are in DECISIONS.

## Next step
T1 (stale `running` jobs), then T2 onwards in order (`/milestone-next`).
