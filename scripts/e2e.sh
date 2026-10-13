#!/usr/bin/env bash
# End-to-end test (PLAN §13, §14): real Chromium (Playwright) against the real production stack with
# TLS_MODE=internal. Uses a temporary env file, data directory and compose project; leaves `.env`
# and `data/` alone. Run via `make e2e`. Needs Docker, Node 20 and network access for the
# Playwright browser download.
set -euo pipefail

cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }

work=$(mktemp -d /tmp/eyetrack-e2e.XXXXXX)  # /tmp is shared with Docker Desktop on macOS; /var/folders is not
project="eyetrack-e2e-$$"
envf="$work/env"
https_port="${E2E_HTTPS_PORT:-19443}"
http_port="${E2E_HTTP_PORT:-19080}"
rand() { python3 -c "import secrets; print(secrets.token_urlsafe(24))"; }
admin_password="$(rand)"

mkdir -p "$work/models" "$work/data"
cat > "$envf" <<ENV
DOMAIN=localhost
TLS_MODE=internal
DATA_DIR=$work/data
MODELS_DIR=$work/models
POSTGRES_PASSWORD=$(rand)
SECRET_KEY=$(rand)
HTTP_PORT=$http_port
HTTPS_PORT=$https_port
ENV

compose() { docker compose -p "$project" --env-file "$envf" -f docker-compose.yml "$@"; }
cleanup() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  # Data was chowned to the container user; remove it as root through a container.
  docker run --rm -u 0 -v "$work:/w" --entrypoint rm eyetrack-backend:local -rf /w/data >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

echo "== start the stack"
compose up -d --build --wait --wait-timeout 600 >/dev/null 2>&1 || {
  compose ps -a; compose logs --tail 40 >&2; fail "stack did not become healthy"
}
printf '%s\n' "$admin_password" | compose exec -T api python cli.py create-admin \
  --username e2e --password-stdin >/dev/null

echo "== test video (3 s, H.264, made with the ffmpeg in the backend image)"
docker run --rm -u "$(id -u):$(id -g)" -v "$work:/w" --entrypoint ffmpeg eyetrack-backend:local \
  -loglevel error -f lavfi -i testsrc=duration=3:size=320x240:rate=25 -pix_fmt yuv420p -c:v libx264 \
  /w/clip.mp4 || fail "could not create the test video"

echo "== browser tests"
# Chained with && on purpose: `set -e` is ignored inside a subshell that is followed by `||`.
install_browser="npx playwright install chromium"
[ -z "${CI:-}" ] || install_browser="npx playwright install --with-deps chromium"
( cd e2e && npm ci >/dev/null && $install_browser >/dev/null \
  && E2E_BASE_URL="https://localhost:$https_port" E2E_ADMIN_USER=e2e E2E_ADMIN_PASSWORD="$admin_password" \
     E2E_VIDEO="$work/clip.mp4" npx playwright test ) \
  || { compose logs --tail 60 api proxy >&2; fail "playwright tests failed"; }

echo "== files on the server match what the admin API shows"
python3 - "$project" "$envf" <<'PY'
import json, subprocess, sys
project, envf = sys.argv[1:3]
def run(*args: str) -> str:
    return subprocess.run(
        ["docker", "compose", "-p", project, "--env-file", envf, "-f", "docker-compose.yml", *args],
        check=True, capture_output=True, text=True).stdout
paths = run("exec", "-T", "api", "sh", "-c",
            "ls /data/studies/*/sessions/*/raw/events.jsonl /data/studies/*/sessions/*/raw/meta.json").split()
events = [p for p in paths if p.endswith("events.jsonl")]
metas = [p for p in paths if p.endswith("meta.json")]
assert len(events) == 3 and len(metas) == 3, paths
lengths = []
for path in events:
    lines = run("exec", "-T", "api", "cat", path).splitlines()
    kinds = [json.loads(line)["type"] for line in lines]
    assert kinds[0] == "session_start", (path, kinds[:5])
    lengths.append(len(kinds))
# Two full sessions, and the reloaded one that stopped early (its events end at the instructions).
assert sorted(lengths)[-2] > 50 and sorted(lengths)[0] >= 2, lengths
for path in metas:
    meta = json.loads(run("exec", "-T", "api", "cat", path))
    assert meta["status"] == "uploaded" and meta["config"]["playlist"], (path, meta)
print(f"{len(events)} sessions: events.jsonl ({sorted(lengths)} events) and meta.json present, status uploaded")
PY
echo "ALL E2E CHECKS PASSED"
