# Webcam eye-tracking for educational video studies

Students watch educational videos in the browser; the app records their webcam, video playback timing and calibration data, uploads it to a Python backend, which estimates gaze offline and produces heatmaps, AOI metrics and attentional synchrony. The full specification is `docs/PLAN.md`.

## Start of every session
1. Read `docs/PROGRESS.md` (current milestone, done tasks, open problems, next step).
2. Read only the `docs/PLAN.md` sections relevant to the current task. Section 1 (principles) always applies.
3. Check `docs/DECISIONS.md` for deviations already agreed.

## Hard rules
- `docs/PLAN.md` is the spec. Never edit it. If something in it is unclear, contradictory or seems wrong, **stop and ask**. Never silently simplify, skip or reinterpret a requirement.
- Agreed deviations go in `docs/DECISIONS.md` (date, plan section, what changed, why, who approved).
- A task is done only when its tests pass and you have shown the test output. "Should work" is not done.
- Do not mock away the hard part: every pipeline stage needs at least one test on real fixture data (real MediaPipe, real ffmpeg).
- Every threshold from the plan is a named config value with the plan's default, never a literal.
- Never touch `data/`, `.env`, or participant recordings. Never commit secrets, model weights or recordings.
- Commit after each finished task: `M<milestone>: <what> (PLAN §x.y)`. One branch per milestone: `m<id>-<slug>`.
- Update `docs/PROGRESS.md` at the end of each task and before ending a session.

## Principles (PLAN §1)
KISS · YAGNI · single source of truth for shared definitions · raw data is immutable · fail loudly · small, pure, typed units (`mypy --strict`, `tsc --strict`) · configuration over constants · measure before optimizing.

## Commands
These exist from Milestone 1a onwards; until then, create them.
- `make dev` — start the dev stack (`compose.dev.yml`)
- `make test` — backend + frontend unit tests
- `make lint` — ruff, mypy, eslint, tsc
- `make e2e` — Playwright end-to-end test with fake camera
- `docker compose exec api python cli.py <command>` — backend CLI

## Layout
`frontend/` TypeScript + Vite (no framework) · `backend/app` FastAPI · `backend/worker` processing pipeline and analysis · `shared/` definitions used by both sides · `docs/` plan, progress, decisions, data formats.

## Workflow
Milestones and their acceptance criteria are in PLAN §14. Use `/milestone-start <id>` to plan a milestone, `/milestone-next` to implement the next approved tasks, and `/milestone-review <id>` before merging. The `plan-reviewer` subagent checks a diff against the plan.

## What needs the human
Anything that requires a real webcam, real lighting or real people (debug-mode checks, moving-dot accuracy test, threshold tuning on pilot data). When a task needs this, finish everything else, then write exactly what the human should test and what to report back.
