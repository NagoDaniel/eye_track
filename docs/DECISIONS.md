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

## 2026-10-12 — HTTP/3 disabled; acceptance platform (T13)
- Plan section: §11.1, §11.4, §11.6
- Decision: the proxy serves HTTP/1.1 and HTTP/2 only (`servers { protocols h1 h2 }`), because the plan publishes only 80 and 443/tcp and Caddy would otherwise advertise HTTP/3 (`alt-svc`) on UDP 443 that is not reachable. The acceptance run (§11.6) used a Hetzner Cloud VPS: Ubuntu 26.04.1 LTS, aarch64, 2 vCPU, 3.7 GB RAM, Docker 29.9.0, Compose 5.6.0; this is outside the plan's named Ubuntu 22.04/24.04 and below its sizing advice (4+ cores, 16 GB), which the README states.
- Reason: avoid advertising an unreachable protocol; record what the acceptance test actually ran on.
- Approved by: plan leaves open

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
- Approved by: (1) Nágó Dániel; rest plan leaves open

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
- Approved by: Champ
