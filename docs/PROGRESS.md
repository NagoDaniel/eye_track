# Progress

Maintained by the implementing agent. Update at the end of every task and before ending a session. Keep it short: this is the hand-over note for the next session.

## Current milestone
**3 — Pipeline A** (PLAN §14): not started; run `/milestone-start 3`. Milestone 2 (Recording and sync data) was accepted by Nágó Dániel and merged into `main` on 2026-10-16; its task list, results, the first real-run fixes and the review are in git history (last commit before the merge: `ca0bbb3`) and in DECISIONS (entries of 2026-10-15 and 2026-10-16).

## Milestone status
| Milestone | Status | Branch | Notes |
|---|---|---|---|
| 1a Infrastructure | done (accepted and merged 2026-10-12) | m1a-infrastructure | task record and review are in git history (`41e299d`) |
| 1b Skeleton app | done (accepted and merged 2026-10-15) | m1b-skeleton-app | PR #2; task record and review fixes in git history (`745c48c`) |
| 2 Recording and sync data | done (accepted and merged 2026-10-16) | m2-recording | reviewed by the plan-reviewer, fixes done except the items under "Open problems"; `make e2e` produces complete raw data (webcam WebM, features, stimulus frames, events) |
| 3 Pipeline A | next, not started | | carries over R2 (stale `running` jobs) from the 1a review; the debug replay video (§12.2) and offline gaze estimation are here |
| 4 Analysis | not started | | |
| 5 Pipeline B and comparison | not started | | |
| 6 Hardening for pilot | not started | | |

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
`/milestone-start 3` (new branch `m3-pipeline-a` from `main`).
