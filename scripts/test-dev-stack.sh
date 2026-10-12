#!/usr/bin/env bash
# Brings up the development stack (compose.dev.yml) and checks it (PLAN §11.5). Run via `make test-dev`.
# Uses a temporary env file, data dir and compose project; leaves `.env` and `data/` alone.
# Briefly edits (and always restores) frontend/src/main.ts and adds backend/app/_dev_reload_probe.py.
set -euo pipefail

cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }

work=$(mktemp -d /tmp/eyetrack-test.XXXXXX)  # /tmp is shared with Docker Desktop on macOS; /var/folders is not
project="eyetrack-dev-test-$$"
envf="$work/env"
port=18081
db_port=15432
base="http://localhost:$port"
rand() { python3 -c "import secrets; print(secrets.token_urlsafe(24))"; }

mkdir -p "$work/models"
node -e 'process.exit(typeof WebSocket === "function" ? 0 : 1)' \
  || fail "the HMR check needs Node 22 or newer on the host (global WebSocket); found $(node --version 2>&1)"
cat > "$envf" <<ENV
DOMAIN=localhost
TLS_MODE=internal
DATA_DIR=$work/data
MODELS_DIR=$work/models
POSTGRES_PASSWORD=$(rand)
SECRET_KEY=$(rand)
DEV_HTTP_PORT=$port
DEV_DB_PORT=$db_port
ENV

main_ts=frontend/src/main.ts
cp "$main_ts" "$work/main.ts.orig"
probe=backend/app/_dev_reload_probe.py

compose() { docker compose -p "$project" --env-file "$envf" -f docker-compose.yml -f compose.dev.yml "$@"; }
cleanup() {
  cp "$work/main.ts.orig" "$main_ts"
  rm -f "$probe"
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  docker run --rm -u 0 -v "$work:/w" --entrypoint rm eyetrack-backend:local -rf /w/data >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

echo "== make dev refuses to run without .env"
if [ ! -f .env ]; then
  out=$(make dev 2>&1) && fail "make dev succeeded without .env"
  echo "$out" | grep -q "no .env found" || fail "unhelpful message: $out"
else
  echo "(skipped: a .env exists in this checkout; not touching it)"
fi

echo "== compose config is valid"
compose config -q || fail "compose config failed"

echo "== up (build, migrate, npm ci, wait for healthy)"
compose up -d --build --wait --wait-timeout 900 >/dev/null 2>&1 || {
  compose ps -a; compose logs --tail 60 >&2; fail "dev stack did not become healthy"
}
compose ps --format '{{.Service}} {{.State}} {{.Health}}'

echo "== plain HTTP, no TLS: proxy routes api, admin and the Vite dev server"
[ "$(curl -s "$base/api/health")" = '{"status":"ok"}' ] || fail "health not ok on $base"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$base/admin/login")" = "200" ] || fail "admin login not served"
curl -s "$base/" | grep -q '/@vite/client' || fail "/ is not served by the Vite dev server"
curl -s "$base/src/main.ts" | grep -q 'document.title' || fail "Vite does not serve source modules"
[ "$(curl -s -o /dev/null -w '%{http_code}' "$base/mediapipe/face_landmarker.task")" = "200" ] \
  || fail "MediaPipe model not served in dev"
curl -s -o /dev/null -w '%{redirect_url}' "http://localhost:$port/" | grep -q . && fail "dev proxy redirects to https"
echo "ok"

echo "== videos are served with range requests"
compose exec -T api sh -c 'mkdir -p /data/videos/1 && ffmpeg -loglevel error -f lavfi -i testsrc=duration=2:size=160x120:rate=25 -pix_fmt yuv420p -c:v libx264 /data/videos/1/video.mp4'
[ "$(curl -s -o /dev/null -w '%{http_code}' -r 0-99 "$base/media/videos/1.mp4")" = "206" ] || fail "no 206 for a range request"

echo "== login works over plain http (cookie is not Secure in dev)"
printf 'a-long-test-password\n' | compose exec -T api python cli.py create-admin --username root --password-stdin
jar="$work/jar"
token=$(curl -s -c "$jar" "$base/admin/login" | grep -o 'name="csrf_token" value="[^"]*"' | cut -d'"' -f4)
[ -n "$token" ] || fail "no CSRF token"
code=$(curl -s -o /dev/null -w '%{http_code}' -b "$jar" -c "$jar" --data-urlencode "username=root" \
  --data-urlencode "password=a-long-test-password" --data-urlencode "csrf_token=$token" "$base/admin/login")
[ "$code" = "303" ] || fail "login returned $code"
[ "$(curl -s -b "$jar" "$base/admin/api/me")" = '{"username":"root"}' ] || fail "session not accepted over http"
curl -s -D - -o /dev/null "$base/admin/login" | grep -i '^set-cookie:' | grep -qi 'secure' \
  && fail "dev cookie is Secure and would not work over http"
echo "ok"

echo "== Postgres is published on localhost only"
python3 -c "import socket; socket.create_connection(('127.0.0.1', $db_port), timeout=5).close()" \
  || fail "cannot reach Postgres on 127.0.0.1:$db_port"
docker inspect -f '{{json .NetworkSettings.Ports}}' "$(compose ps -q db)" | grep -q '"HostIp":"127.0.0.1"' \
  || fail "db is not bound to 127.0.0.1"
echo "ok  127.0.0.1:$db_port"

echo "== editing backend code reloads the api"
before=$(compose logs api --no-log-prefix 2>&1 | grep -c "Application startup complete" || true)
cat > "$probe" <<'PY'
PROBE = "reload me"
PY
for _ in $(seq 1 30); do
  after=$(compose logs api --no-log-prefix 2>&1 | grep -c "Application startup complete" || true)
  [ "$after" -gt "$before" ] && break
  sleep 1
done
[ "$after" -gt "$before" ] || { compose logs api --tail 20 >&2; fail "api did not reload after a source change"; }
for _ in $(seq 1 20); do
  [ "$(curl -s "$base/api/health")" = '{"status":"ok"}' ] && break
  sleep 1
done
[ "$(curl -s "$base/api/health")" = '{"status":"ok"}' ] || fail "api unhealthy after reload"
echo "ok  api reloaded and is healthy"

echo "== editing frontend code sends an HMR update over the websocket (through the proxy)"
cat > "$work/hmr.mjs" <<'JS'
import { appendFileSync } from "node:fs";

const [base, file] = process.argv.slice(2);
const client = await (await fetch(`${base}/@vite/client`)).text();
const token = /wsToken = "([^"]+)"/.exec(client)?.[1];
if (!token) throw new Error("no HMR token found in /@vite/client");
const ws = new WebSocket(`${base.replace("http", "ws")}/?token=${token}`, "vite-hmr");
const deadline = setTimeout(() => { console.error("no HMR update within 20 s"); process.exit(1); }, 20000);
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.type === "connected") {
    appendFileSync(file, "\n// hmr probe\n");
  } else if (msg.type === "update" || msg.type === "full-reload") {
    console.log(`received ${msg.type}`);
    clearTimeout(deadline);
    process.exit(0);
  }
};
ws.onerror = () => { console.error("websocket error"); process.exit(1); };
JS
node "$work/hmr.mjs" "$base" "$main_ts" || fail "HMR check failed"
cp "$work/main.ts.orig" "$main_ts"

echo "ALL DEV STACK CHECKS PASSED"
