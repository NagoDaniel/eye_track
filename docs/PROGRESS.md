# Progress

Maintained by the implementing agent. Update at the end of every task and before ending a session. Keep it short: this is the hand-over note for the next session.

## Current milestone
**1a — Infrastructure** (PLAN §14), branch `m1a-infrastructure`. Next: T3.

## Milestone status
| Milestone | Status | Branch | Notes |
|---|---|---|---|
| 1a Infrastructure | in progress | m1a-infrastructure | |
| 1b Skeleton app | not started | | |
| 2 Recording and sync data | not started | | |
| 3 Pipeline A | not started | | |
| 4 Analysis | not started | | |
| 5 Pipeline B and comparison | not started | | |
| 6 Hardening for pilot | not started | | |

## Approved task list (current milestone)
Approved 2026-10-12. Each task ends with tests passing + output shown, then a commit `M1a: <what> (PLAN §x.y)`.

- [x] **T1. Repo skeleton + tooling** (§3, §11.5, §13). Backend `pyproject.toml` (uv + lock, ruff, mypy --strict, pytest), frontend `package.json`/strict `tsconfig`/eslint/vitest, `shared/`, `.gitignore` (data/, .env, models weights), `Makefile` (`dev test lint e2e`; `e2e` fails loudly "not implemented until M2"). Test: `make lint`, `make test` green on one smoke test each side.
- [x] **T2. Settings + structured JSON logging** (§12.3). `settings.py` (all `.env.example` keys, fail loudly on missing), JSON logger with `session_id`/`job_id`/`stage`, `LOG_LEVEL`. Tests: log line is valid JSON with context fields; settings reject missing vars.
- [ ] **T3. DB models + Alembic migration** (§4, §6). All §4 entities + `AdminUser` + worker heartbeat table. Tests on real Postgres 16: upgrade head / downgrade base, Chunk PK, status enums.
- [ ] **T4. Job queue core** (§4 Job, §11.1). `jobs.py` enqueue/claim (`SKIP LOCKED`), `worker/run.py` loop + 30 s heartbeat. Tests: concurrent claimers never share a job (real Postgres); heartbeat updates.
- [ ] **T5. FastAPI app + `/api/health`** (§11.1). DB-checking health (503 when DB down), request logging. Tests: 200 / 503.
- [ ] **T6. Admin auth + `create-admin` CLI** (§6, §10). argon2, cookie login/logout (12 h, HttpOnly/Secure/SameSite), CSRF, Jinja login + landing page, admin JSON under `/admin/api/*`, Typer `cli.py create-admin`. Tests: bad/good password, protected route, CSRF 403, CLI creates argon2 hash, duplicate fails.
- [ ] **T7. Backend Dockerfile** (§11.1). Multi-stage, python:3.11-slim + ffmpeg + libgl1/libglib2.0-0, uv from lock, non-root, `face_landmarker.task` downloaded at build + sha256-verified. Tests: image builds; `import mediapipe, cv2, av` and `ffmpeg -version` in image; wrong sha256 fails build.
- [ ] **T8. Proxy image + Caddyfile** (§11.1, §11.2, §10). Node 20 builds placeholder Vite page **with MediaPipe WASM + `face_landmarker.task` bundled into `dist/`** (see DECISIONS); caddy:2; `/api` `/admin` proxied, `/media/videos` range support, other paths → static SPA fallback, TLS modes acme/internal/files, HSTS, `Permissions-Policy`, CSP allowing WASM, body limit. Tests: `caddy validate` all 3 modes; curl checks headers, 206 on fixture MP4, oversized body rejected, bundled WASM/model served.
- [ ] **T9. `docker-compose.yml` + `.env.example`** (§11.1, §11.2). 5 services, `service_completed_successfully` deps, healthchecks (api, db, worker heartbeat script), restart, log rotation, only proxy publishes ports, volumes, documented `.env.example`. **No GPU profile** (deferred to M5). Tests: `docker compose config`; stack healthy with `TLS_MODE=internal`; `create-admin` + admin login over HTTPS; db not reachable from host; worker starts with empty `models/`.
- [ ] **T10. `compose.dev.yml` + `make dev`** (§11.5). Vite HMR, api `--reload`, Postgres on localhost, no TLS. Test: `/api/health` 200 on localhost; py edit reloads api.
- [ ] **T11. `scripts/setup.sh`** (§11.3). Interactive + non-interactive flags; Docker version check; random secrets if `change-me`; DNS check for acme; wait for healthy; admin prompt. Tests: shellcheck; secrets generated, existing values untouched, old Docker fails loudly; full non-interactive run on `internal` TLS ends healthy with working admin login.
- [ ] **T12. Other scripts + CI** (§11.4, §11.5). `fetch_models.sh` (manifest-driven, sha256 check; manifest entries for resnet50_gaze.onnx and optional mobileone_s0_gaze.onnx per DECISIONS), `backup.sh` (`--no-raw-video`), `restore.sh`; GitHub Actions (lint, mypy, tsc, unit tests, build images, compose smoke test health + login; E2E added in M2). Tests: backup→wipe→restore round trip identical (sha256); `fetch_models.sh` rejects wrong hash; `actionlint`.
- [ ] **T13. README + acceptance** (§11.3, §11.4, §11.6). README (setup, ops, CLI, sizing, firewall, restore test). **Human test** on Hetzner VPS with `nagodaniel.com` subdomain, `TLS_MODE=acme`.

### "Done when" mapping
`scripts/setup.sh` on fresh VM → healthy HTTPS instance, admin can log in: automated by T9 + T11 (internal TLS); real acme run on fresh Hetzner VM by T13 (human).

## Done (current milestone)
- T1 repo skeleton + tooling. `make lint` and `make test` green (1 pytest, 1 vitest); `make e2e` fails loudly until M2. Note: `make test`/`lint` run on the host via uv/npm for now (PLAN §11.5 says they wrap compose); switch to compose once images exist (T7+). Python pinned 3.11 (`.python-version`; mediapipe has no 3.14 wheels).

- T2 settings + JSON logging. 13 new pytest tests pass (`make lint`, `make test` green). `Settings` rejects missing/`change-me` `SECRET_KEY` and `POSTGRES_PASSWORD` always, so dev needs a real `.env` too (T10). Backend settings read `DATA_ROOT`/`MODELS_ROOT` (container paths `/data`, `/models`); host-side `DATA_DIR`/`MODELS_DIR`, TLS and DOMAIN keys are compose/Caddy only.

## Open problems / questions for the human
_None._

## Needs human testing
- T13: start Hetzner VPS, point a DNS A record (e.g. `eyetrack.nagodaniel.com`) at it, run README steps + `scripts/setup.sh`. Exact checklist to be written at T13.

## Next step
T3: DB models + Alembic migration (needs real Postgres 16; Docker available locally).
