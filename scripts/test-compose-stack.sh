#!/usr/bin/env bash
# Brings up the full production stack with TLS_MODE=internal and checks it (PLAN §11.1, §11.2).
# Uses a temporary env file, data directory and compose project; leaves `.env` and `data/` alone.
# Run via `make test-stack`.
set -euo pipefail

cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }

work=$(mktemp -d /tmp/eyetrack-test.XXXXXX)  # /tmp is shared with Docker Desktop on macOS; /var/folders is not
project="eyetrack-test-$$"
envf="$work/env"
https_port=18443
http_port=18080
rand() { python3 -c "import secrets; print(secrets.token_urlsafe(24))"; }

mkdir -p "$work/models"   # empty on purpose: the worker must start without weights
cat > "$envf" <<ENV
DOMAIN=localhost
TLS_MODE=internal
ACME_EMAIL=ops@example.org
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

echo "== compose file is valid; missing secrets fail loudly"
compose config -q || fail "docker compose config failed"
grep -v '^SECRET_KEY=' "$envf" > "$work/env-nosecret"
if docker compose -p "$project" --env-file "$work/env-nosecret" config -q 2>/dev/null; then
  fail "compose accepted a missing SECRET_KEY"
fi
grep -v '^POSTGRES_PASSWORD=' "$envf" > "$work/env-nopw"
if docker compose -p "$project" --env-file "$work/env-nopw" config -q 2>/dev/null; then
  fail "compose accepted a missing POSTGRES_PASSWORD"
fi

echo "== only the proxy publishes ports"
compose config --format json | python3 -c "
import json, sys
services = json.load(sys.stdin)['services']
published = sorted(n for n, s in services.items() if s.get('ports'))
assert published == ['proxy'], published
assert sorted(services) == ['api', 'db', 'migrate', 'proxy', 'worker'], sorted(services)
print('services:', ', '.join(sorted(services)), '| published by:', published[0])
"

echo "== up (build, migrate, wait for healthy)"
compose up -d --build --wait --wait-timeout 600 >/dev/null 2>&1 || {
  compose ps -a; compose logs --tail 40 >&2; fail "stack did not become healthy"
}
compose ps --format '{{.Service}} {{.State}} {{.Health}}'

echo "== migrate exited 0; every long-running service is healthy"
[ "$(compose ps -a migrate --format '{{.ExitCode}}')" = "0" ] || fail "migrate did not exit 0"
for svc in db api worker proxy; do
  health=$(compose ps "$svc" --format '{{.Health}}')
  [ "$health" = "healthy" ] || fail "$svc is '$health'"
done

echo "== restart policy, log rotation, non-root"
for svc in db api worker proxy migrate; do
  cid=$(compose ps -a -q "$svc")
  want="unless-stopped"; [ "$svc" = "migrate" ] && want="no"
  got=$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$cid")
  [ "$got" = "$want" ] || fail "$svc restart policy '$got', expected '$want'"
  docker inspect -f '{{.HostConfig.LogConfig.Type}} {{index .HostConfig.LogConfig.Config "max-size"}} {{index .HostConfig.LogConfig.Config "max-file"}}' "$cid" \
    | grep -qx "json-file 20m 5" || fail "$svc log rotation not json-file 20m 5"
done
for svc in api worker; do
  [ "$(compose exec -T "$svc" id -u)" = "10001" ] || fail "$svc is not running as uid 10001"
done
echo "ok"

echo "== data directories are writable by the app user; models dir is empty and mounted read-only"
# shellcheck disable=SC2016  # $d must expand inside the container, not here
compose exec -T api sh -c 'for d in videos studies analysis; do touch /data/$d/.w && rm /data/$d/.w || exit 1; done' \
  || fail "app user cannot write to /data"
[ -z "$(ls -A "$work/models")" ] || fail "models dir not empty"
if compose exec -T worker sh -c 'touch /models/x' 2>/dev/null; then fail "/models is writable"; fi
# The admin pages check for the weights too, so the api container must see the same directory.
compose exec -T api test -d /models || fail "the api container has no /models"
if compose exec -T api sh -c 'touch /models/x' 2>/dev/null; then fail "/models is writable in api"; fi

echo "== worker runs without model weights and its heartbeat is fresh"
compose exec -T worker python -m worker.healthcheck || fail "worker healthcheck failed"
worker_log="$(compose logs worker --no-log-prefix 2>&1)"  # captured: an early-exiting grep would break the pipe
echo "$worker_log" | grep -q '"msg": "worker started"' || fail "worker did not log startup"

echo "== api logs are JSON"
compose logs api --no-log-prefix 2>&1 | python3 -c "
import json, sys
lines = [l for l in sys.stdin.read().splitlines() if l.strip()]
bad = [l for l in lines if not l.startswith('{')]
assert lines and not bad, bad[:3]
[json.loads(l) for l in lines]
print(len(lines), 'api log lines, all JSON')
"

echo "== database is not reachable from the host"
for svc in db api worker; do
  bindings=$(docker inspect -f '{{json .NetworkSettings.Ports}}' "$(compose ps -q "$svc")")
  echo "$bindings" | grep -q 'HostPort' && fail "$svc publishes a port to the host: $bindings"
done
echo "ok  db, api and worker have no host port bindings"

echo "== HTTPS works end to end: health, create-admin, login"
base="https://localhost:$https_port"
[ "$(curl -sk "$base/api/health")" = '{"status":"ok"}' ] || fail "health through the proxy is not ok"
printf 'a-long-test-password\n' | compose exec -T api python cli.py create-admin \
  --username root --password-stdin
jar="$work/jar"
token=$(curl -sk -c "$jar" "$base/admin/login" | grep -o 'name="csrf_token" value="[^"]*"' | cut -d'"' -f4)
[ -n "$token" ] || fail "no CSRF token on the login page"
resp=$(curl -sk -o /dev/null -w '%{http_code} %{redirect_url}' -b "$jar" -c "$jar" \
  --data-urlencode "username=root" --data-urlencode "password=a-long-test-password" \
  --data-urlencode "csrf_token=$token" "$base/admin/login")
[ "$resp" = "303 $base/admin/" ] || fail "login response: $resp"
[ "$(curl -sk -b "$jar" "$base/admin/api/me")" = '{"username":"root"}' ] || fail "admin session not accepted"
[ "$(curl -sk -o /dev/null -w '%{http_code}' "$base/admin/api/me")" = "401" ] || fail "anonymous admin access allowed"
curl -sk -D - -o /dev/null "$base/admin/login" | grep -i '^set-cookie:' | grep -qi 'secure' \
  || fail "session cookie is not Secure"
echo "ok  admin 'root' logged in over HTTPS"

echo "== data survives a restart of db and api"
compose restart db api >/dev/null 2>&1
for _ in $(seq 1 60); do
  [ "$(compose ps api --format '{{.Health}}')" = "healthy" ] && break
  sleep 2
done
[ "$(curl -sk -b "$jar" "$base/admin/api/me")" = '{"username":"root"}' ] || fail "session lost after restart"
compose exec -T api python -c "
from app.db import make_engine, make_session_factory
from app.models import AdminUser
from app.settings import Settings
with make_session_factory(make_engine(Settings().database_url))() as db:
    assert db.query(AdminUser).count() == 1
print('admin row still present')
"

echo "ALL STACK CHECKS PASSED"
