# eye_track

Webcam eye tracking for educational video studies. Students watch videos in the browser, their webcam is recorded, and a Python backend estimates where they looked and produces heatmaps and area-of-interest metrics.

**Status:** Milestone 1a (infrastructure) is implemented: the Docker stack, HTTPS, database, job queue, admin login and operations scripts. The participant app, the admin pages for studies and videos, recording and gaze estimation come in later milestones; see [`docs/PROGRESS.md`](docs/PROGRESS.md).

- Specification: [`docs/PLAN.md`](docs/PLAN.md)
- Progress and open questions: [`docs/PROGRESS.md`](docs/PROGRESS.md)
- Decisions and deviations from the plan: [`docs/DECISIONS.md`](docs/DECISIONS.md)
- Working with Claude Code: [`CLAUDE.md`](CLAUDE.md)

---

## 1. Install on a server

### What you need

| | |
|---|---|
| Server | Linux with a public IP. Ubuntu 22.04 / 24.04 per the plan; the acceptance run used **Ubuntu 26.04 on ARM64** (Hetzner). |
| Software | **Docker Engine 24+** and the **Compose plugin 2.24+**. Nothing else is installed on the host. On Ubuntu or Debian, `sudo ./scripts/install-docker.sh` installs both from Docker's official repository. |
| Size | For a real study: 4+ CPU cores, 16 GB RAM. Disk is about 150 MB per 10-minute session for raw plus derived data (50 students × 10 sessions ≈ 75 GB), plus room for analysis videos. A 2 vCPU / 4 GB machine is enough to try the system out. |
| Domain | A DNS name pointing at the server (for `TLS_MODE=acme`). |
| Firewall | Open **80 and 443 (TCP)** to the internet, plus SSH for you. Nothing else: the database and the API are never published. |

### Quick start

```bash
git clone <repo-url> eyetrack && cd eyetrack
sudo ./scripts/install-docker.sh        # only if Docker is not installed yet (Ubuntu/Debian)
./scripts/setup.sh
```

`scripts/setup.sh` asks for your domain and TLS mode, creates `.env` with random secrets, checks that the domain points at this server, builds and starts everything, waits until all healthchecks pass, and creates the first admin account. The first build downloads several hundred MB and takes a few minutes. It is safe to run again: secrets and existing admins are kept and the stack is updated in place.

When it finishes it prints the admin login address, `https://<your domain>/admin/login`.

### The same steps by hand

```bash
git clone <repo-url> eyetrack && cd eyetrack
cp .env.example .env
nano .env                          # at least DOMAIN, and both "change-me" secrets (see below)
docker compose up -d --build --wait
docker compose exec api python cli.py create-admin --username <you>
```

Set `POSTGRES_PASSWORD` and `SECRET_KEY` to long random strings (for example `openssl rand -base64 36`) **before the first start**: the database keeps the password it was created with, and the application refuses to start while a secret is still `change-me`. `./scripts/setup.sh --env-only` creates `.env` with random secrets and nothing else.

Every setting is documented line by line in [`.env.example`](.env.example).

### TLS: three modes

HTTPS is mandatory because browsers only allow camera access on secure origins. Set `TLS_MODE` in `.env`:

| Mode | Use it when | What you do |
|---|---|---|
| `acme` | The server has a public domain and ports 80 and 443 are reachable from the internet. | Nothing: Caddy requests and renews a Let's Encrypt certificate. `ACME_EMAIL` is optional and only receives expiry reminders. |
| `files` | Your university IT gives you a certificate. | Set `TLS_CERT_FILE` and `TLS_KEY_FILE` to the **host paths** of the two files; they are mounted read-only into the proxy. |
| `internal` | A lab network without a public domain. | Caddy uses its own local certificate authority. Every browser must trust it, see below. |

**Cloudflare:** if your DNS is on Cloudflare, set the record to **DNS only** (grey cloud). With the proxy on, Cloudflare decrypts all traffic, including participants' webcam video, which a study's ethics approval must explicitly allow; it also caps request sizes and breaks the automatic certificate.

**Trusting the internal CA** (`TLS_MODE=internal`): copy the root certificate off the server and install it as a trusted authority on every machine that opens the site.

```bash
docker compose cp proxy:/caddy/data/caddy/pki/authorities/local/root.crt ./eyetrack-root.crt
```

### Check that it works

```bash
docker compose ps                                  # every service "healthy", migrate "exited (0)"
curl https://<your domain>/api/health              # {"status":"ok"}
```

Then open `https://<your domain>/admin/login` and log in.

---

## 2. Operating it

All commands run in the project directory on the server.

| Task | Command |
|---|---|
| Update | `git pull && docker compose up -d --build` (migrations run automatically through the `migrate` service) |
| Status | `docker compose ps` |
| Logs (JSON lines) | `docker compose logs -f api` (also `worker`, `proxy`, `db`) |
| Create an admin | `docker compose exec api python cli.py create-admin --username <name>` |
| Stop / start | `docker compose stop` / `docker compose up -d` |
| Pipeline B model weights | `./scripts/fetch_models.sh` (default: ResNet-50) or `./scripts/fetch_models.sh l2cs_mobileone_s0` |

Containers restart automatically (`restart: unless-stopped`) and Docker starts at boot, so the stack comes back after a reboot. Logs rotate at 20 MB × 5 files per container.

**Where the data lives.** `DATA_DIR` (default `./data`) holds `videos/`, `studies/` (raw and derived session data) and `analysis/`. It contains participants' webcam recordings: **put it on an encrypted volume**. The application does not encrypt anything itself. The PostgreSQL data is in the Docker volume `pgdata`. Caddy's certificates are in the volume `caddy_data`.

**Model weights and licence.** The Pipeline B weights are trained on Gaze360, whose terms restrict use to **non-commercial research**. This is acceptable for an academic study. Weights are not committed; they are downloaded and checked against a sha256 by `fetch_models.sh`. The system works without them; only Pipeline B jobs fail, with a clear message. The sources and hashes are in `shared/models.json` and `docs/DECISIONS.md`.

### Running a study (admin)

Log in at `https://<your domain>/admin/login`.

1. **Videos**: upload the stimulus MP4 (title + file). H.264 with a constant frame rate is best; other files are accepted with a warning. A video that a study or an existing session uses cannot be deleted.
2. **Studies**: create a study with a name, a short code (letters, digits, `-`, `_`; 3 to 32 characters), the mode, and the config JSON (every key is optional, defaults are in PLAN §4.1; `playlist` lists the video ids as strings, for example `{"playlist": ["1"]}`). The study page shows the participant link `https://<your domain>/s/<code>`; adding `?p=AB0315` prefills the participant code.
3. **Participants** open the link, enter their code (first two letters of the name, birth month, birth day), accept the consent text, and follow the screens. Without a camera in this version the session shows the dots and the video and uploads the events.
4. **Sessions**: lists every session with its status, flags and chunk counts per stream; the detail page shows gaps in the uploaded chunks and the events received.

The consent text is a plain-text file per version: `backend/app/consent/<consent_version>.txt` (`v1` is a placeholder, to be replaced by the text the ethics committee approves). A new version needs a rebuild: `docker compose up -d --build`. Formats of everything stored are in [`docs/DATA_FORMATS.md`](docs/DATA_FORMATS.md).

Limits (all in `.env`, see `.env.example`): `MAX_UPLOAD_CHUNK_MB`, `MAX_VIDEO_UPLOAD_MB`, `MAX_ADMIN_REQUEST_MB`, `MAX_SESSION_TOTAL_MB`, `RATE_LIMIT_PER_HOUR` (session creations and error reports per network address; the default is high so that only bots hit it).

### Backup and restore

```bash
./scripts/backup.sh /path/to/backups                    # database + all data
./scripts/backup.sh --no-raw-video /path/to/backups     # without the raw webcam files
```

This creates `eyetrack-backup-<timestamp>/` containing `db.dump`, `data.tar.gz` and `MANIFEST.txt` (counts and sha256 of both files). The stack must be running. The backup contains participant recordings, so keep it encrypted and access-controlled. `.env` (the secrets) is **not** included: store it separately.

```bash
./scripts/restore.sh /path/to/backups/eyetrack-backup-<timestamp>
```

`restore.sh` verifies both checksums first, refuses to overwrite a target that already holds data unless you pass `--force`, asks you to type the project name, restores the database in one transaction, extracts the data, restarts the stack and compares row counts with the manifest. To move to a new server: install as in section 1, then run `restore.sh`.

**Test your backups before you need them.** A restore test on the same machine, without touching the live system:

```bash
cp .env /tmp/restore-test.env
# edit /tmp/restore-test.env: DATA_DIR=/tmp/restore-test-data, HTTP_PORT=18080, HTTPS_PORT=18443,
# TLS_MODE=internal, DOMAIN=localhost; keep or change the secrets (the test stack is separate)
mkdir -p /tmp/restore-test-data
./scripts/setup.sh --env-file /tmp/restore-test.env --project restore-test --non-interactive --no-admin
./scripts/restore.sh --env-file /tmp/restore-test.env --project restore-test --yes /path/to/backups/eyetrack-backup-<timestamp>
# open https://localhost:18443/admin/login, log in with an admin from the backup, check the data
docker compose -p restore-test --env-file /tmp/restore-test.env down -v    # clean up
sudo rm -rf /tmp/restore-test-data /tmp/restore-test.env   # the data was chowned to the container user
```

The repository's automated version of this (`make test-backup`) destroys a seeded stack and checks that every database row and every file comes back identical.

### Troubleshooting

| Symptom | Likely cause |
|---|---|
| `api` exits with "must be set to a real secret" | `POSTGRES_PASSWORD` or `SECRET_KEY` is still `change-me` in `.env`. |
| `setup.sh` says the database volume already exists | The database was initialised with another password. Restore the original `POSTGRES_PASSWORD`, or remove the volume with `docker compose down -v` if there is nothing to keep. |
| No certificate / browser warning (`acme`) | DNS does not point at the server, ports 80 or 443 are blocked (check the cloud provider's firewall too), or Cloudflare's proxy is on. See `docker compose logs proxy`. |
| `/api/health` returns 503 | The API cannot reach PostgreSQL: `docker compose logs db api`. |
| `worker` unhealthy | Its heartbeat is older than 90 s: `docker compose logs worker`. |

---

## 3. Development

Needs Docker, Python 3.11 with [uv](https://docs.astral.sh/uv/), Node 20, and `ffmpeg` with `ffprobe` on the host (the video upload tests run the real tools; `brew install ffmpeg` or `apt install ffmpeg`).

```bash
./scripts/setup.sh --env-only      # once: creates .env with random secrets
make dev                           # http://localhost:8080, hot reload for frontend and api
make lint                          # ruff, mypy --strict, eslint, tsc --strict
make test                          # backend (real PostgreSQL in a container) and frontend unit tests
```

Slower suites that build and start real containers (they use temporary directories and never touch your `.env` or `data/`):

| Command | Checks |
|---|---|
| `make test-images` | backend and proxy images: non-root, real MediaPipe loads the pinned model, proxy routes, headers, limits |
| `make test-stack` | the production stack with `TLS_MODE=internal`: healthchecks, ports, admin login over HTTPS |
| `make test-dev` | the development stack: hot reload for api and frontend (needs Node 22+ on the host for the HMR websocket check) |
| `make test-setup` | `scripts/setup.sh`: prompts, secrets, DNS check, idempotent re-run |
| `make test-backup` | backup, destroy everything, restore, compare every row and file |
| `make test-fetch-models` | verified model download |
| `make test-install-docker` | `scripts/install-docker.sh` in fresh Ubuntu and Debian containers |
| `make e2e` | Playwright end-to-end test: real Chromium against the real stack, a full dummy session |

CI (`.github/workflows/ci.yml`) runs the same targets.

### Layout

```
frontend/   TypeScript + Vite (no framework)
backend/    app/ (FastAPI), worker/ (job loop), alembic/ (migrations), cli.py
shared/     definitions used by both sides (protocol.json: streams, event types, config defaults, example payloads; models.json)
e2e/        Playwright end-to-end test
proxy/      Caddy image, entrypoint and shared routes
scripts/    setup, backup, restore, fetch_models and their tests
docs/       plan, progress, decisions
```
