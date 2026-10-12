#!/usr/bin/env bash
# Tests scripts/setup.sh (PLAN §11.3). Run via `make test-setup`.
# Works on a temporary env file, data dir and compose project; never touches `.env` or `data/`.
set -euo pipefail

cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }

work=$(mktemp -d)
project="eyetrack-setup-test-$$"
envf="$work/env"
https_port=18443
http_port=18080

cleanup() {
  docker compose -p "$project" --env-file "$envf" -f docker-compose.yml down -v --remove-orphans >/dev/null 2>&1 || true
  docker volume rm "${project}_pgdata" >/dev/null 2>&1 || true
  docker run --rm -u 0 -v "$work:/w" --entrypoint rm eyetrack-backend:local -rf /w/data >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

setup() { ./scripts/setup.sh --env-file "$envf" --project "$project" "$@"; }
get() { grep -E "^$1=" "$envf" | tail -1 | cut -d= -f2-; }
put() { sed -i.bak "s|^$1=.*|$1=$2|" "$envf" && rm -f "$envf.bak"; }
# expect_fail PATTERN cmd...: command must exit non-zero and print PATTERN
expect_fail() {
  local pattern=$1; shift
  local out
  if out=$("$@" 2>&1); then fail "expected failure, but it succeeded: $*"; fi
  printf '%s' "$out" | grep -q "$pattern" || fail "wrong error (wanted '$pattern'): $out"
}
fresh_env() {
  cp .env.example "$envf"
  put DATA_DIR "$work/data"; put MODELS_DIR "$work/models"
  put HTTP_PORT "$http_port"; put HTTPS_PORT "$https_port"
}
mkdir -p "$work/models"

echo "== shellcheck"
if docker run --rm -v "$PWD:/mnt" koalaman/shellcheck:stable scripts/setup.sh scripts/test-setup.sh >/dev/null 2>&1; then
  echo "ok"
else
  docker run --rm -v "$PWD:/mnt" koalaman/shellcheck:stable scripts/setup.sh scripts/test-setup.sh >&2 || true
  fail "shellcheck reported problems"
fi

echo "== --help works; unknown options and missing admin choice are rejected"
./scripts/setup.sh --help | grep -q -- "--non-interactive" || fail "help text missing"
expect_fail "unknown option" ./scripts/setup.sh --bogus
expect_fail "needs --admin-password-stdin or --no-admin" setup --non-interactive

echo "== old Docker or Compose fails loudly before anything is created"
rm -f "$envf"
SETUP_MIN_DOCKER=99 expect_fail "Docker 99 or newer required" setup --non-interactive --no-admin
SETUP_MIN_COMPOSE=99 expect_fail "Compose 99 or newer required" setup --non-interactive --no-admin
[ ! -e "$envf" ] || fail "env file was created although the version check failed"

echo "== --env-only creates the env file with random secrets and needs no Docker"
env PATH=/usr/bin:/bin ./scripts/setup.sh --env-file "$envf" --env-only 2>/dev/null
for key in POSTGRES_PASSWORD SECRET_KEY; do
  v=$(get "$key")
  [ "$v" != "change-me" ] && [ "${#v}" -ge 32 ] || fail "$key not generated: '$v'"
done
[ "$(get POSTGRES_PASSWORD)" != "$(get SECRET_KEY)" ] || fail "secrets are identical"
mode=$(stat -f '%Lp' "$envf" 2>/dev/null || stat -c '%a' "$envf")
[ "$mode" = "600" ] || fail ".env permissions are $mode, expected 600"
echo "ok"

echo "== existing secrets are never changed, only the placeholder is replaced"
pw=$(get POSTGRES_PASSWORD)
put SECRET_KEY change-me
put POSTGRES_PASSWORD my-own-database-password
env PATH=/usr/bin:/bin ./scripts/setup.sh --env-file "$envf" --env-only 2>/dev/null
[ "$(get POSTGRES_PASSWORD)" = "my-own-database-password" ] || fail "existing password was overwritten"
[ "$(get SECRET_KEY)" != "change-me" ] || fail "placeholder SECRET_KEY was not replaced"
[ "$pw" != "$(get POSTGRES_PASSWORD)" ]
sk=$(get SECRET_KEY)
env PATH=/usr/bin:/bin ./scripts/setup.sh --env-file "$envf" --env-only 2>/dev/null
[ "$(get SECRET_KEY)" = "$sk" ] || fail "second run changed SECRET_KEY"
echo "ok"

echo "== configuration errors are reported"
fresh_env
expect_fail "still the example value" setup --non-interactive --no-admin --dry-run
put DOMAIN localhost
put TLS_MODE bogus
expect_fail "TLS_MODE must be one of" setup --non-interactive --no-admin --dry-run
put TLS_MODE acme
expect_fail "needs your own ACME_EMAIL" setup --non-interactive --no-admin --dry-run
put TLS_MODE files
expect_fail "needs TLS_CERT_FILE" setup --non-interactive --no-admin --dry-run
: > "$work/empty.pem"
put TLS_CERT_FILE "$work/empty.pem"; put TLS_KEY_FILE "$work/empty.pem"
expect_fail "non-empty file" setup --non-interactive --no-admin --dry-run
put DOMAIN "bad domain!"
put TLS_MODE internal
expect_fail "not a valid host name" setup --non-interactive --no-admin --dry-run
echo "ok"

echo "== DNS check (TLS_MODE=acme)"
put TLS_MODE acme; put ACME_EMAIL ops@example.org
put DOMAIN nonexistent.invalid
expect_fail "does not resolve" setup --non-interactive --no-admin --dry-run
put DOMAIN 8.8.8.8
expect_fail "this server has" setup --non-interactive --no-admin --dry-run
setup --non-interactive --no-admin --dry-run --skip-dns-check >/dev/null 2>&1 || fail "--skip-dns-check did not skip"
expect_fail "aborted" bash -c "printf '\n\n\nn\n' | ./scripts/setup.sh --env-file '$envf' --project '$project' --no-admin --dry-run"
printf '\n\n\ny\n' | setup --no-admin --dry-run >/dev/null 2>&1 || fail "interactive 'y' did not continue"
put DOMAIN localhost
setup --non-interactive --no-admin --dry-run >/dev/null 2>&1 || fail "a domain resolving to this machine was rejected"
echo "ok"

echo "== a database volume with a placeholder password is refused"
fresh_env; put DOMAIN localhost; put TLS_MODE internal
docker volume create "${project}_pgdata" >/dev/null
expect_fail "already exists but POSTGRES_PASSWORD is not set" setup --non-interactive --no-admin --dry-run
docker volume rm "${project}_pgdata" >/dev/null
echo "ok"

echo "== full interactive run: prompts, secrets, start, admin"
fresh_env
answers=$(printf 'localhost\ninternal\nroot\na-long-test-password\na-long-test-password\n')
printf '%s\n' "$answers" | setup >"$work/run1.log" 2>&1 || { tail -40 "$work/run1.log" >&2; fail "interactive setup failed"; }
[ "$(get DOMAIN)" = "localhost" ] && [ "$(get TLS_MODE)" = "internal" ] || fail "prompt answers not saved"
[ "$(get SECRET_KEY)" != "change-me" ] && [ "$(get POSTGRES_PASSWORD)" != "change-me" ] || fail "secrets not generated"
grep -q "Creating admin 'root'" "$work/run1.log" || fail "admin was not created"
base="https://localhost:$https_port"
[ "$(curl -sk "$base/api/health")" = '{"status":"ok"}' ] || fail "stack not healthy over HTTPS"
jar="$work/jar"
token=$(curl -sk -c "$jar" "$base/admin/login" | grep -o 'name="csrf_token" value="[^"]*"' | cut -d'"' -f4)
code=$(curl -sk -o /dev/null -w '%{http_code}' -b "$jar" -c "$jar" --data-urlencode "username=root" \
  --data-urlencode "password=a-long-test-password" --data-urlencode "csrf_token=$token" "$base/admin/login")
[ "$code" = "303" ] || fail "admin login returned $code"
[ "$(curl -sk -b "$jar" "$base/admin/api/me")" = '{"username":"root"}' ] || fail "admin session rejected"
echo "ok  interactive setup produced a healthy HTTPS stack with a working admin"

echo "== second run is idempotent: same secrets, new admin from flags, existing admin kept"
secrets_before="$(get SECRET_KEY)$(get POSTGRES_PASSWORD)"
printf 'another-long-password\n' | setup --non-interactive --admin-username second --admin-password-stdin \
  >"$work/run2.log" 2>&1 || { tail -30 "$work/run2.log" >&2; fail "second run failed"; }
grep -q "Creating admin 'second'" "$work/run2.log" || fail "second admin not created"
printf 'another-long-password\n' | setup --non-interactive --admin-username second --admin-password-stdin \
  >"$work/run3.log" 2>&1 || { tail -30 "$work/run3.log" >&2; fail "third run failed"; }
grep -q "already exists; left unchanged" "$work/run3.log" || fail "existing admin was not reported as kept"
[ "$secrets_before" = "$(get SECRET_KEY)$(get POSTGRES_PASSWORD)" ] || fail "re-run changed the secrets"
[ "$(curl -sk "$base/api/health")" = '{"status":"ok"}' ] || fail "stack unhealthy after re-runs"
[ "$(curl -sk -b "$jar" "$base/admin/api/me")" = '{"username":"root"}' ] || fail "first admin session lost"
echo "ok"

echo "ALL SETUP CHECKS PASSED"
