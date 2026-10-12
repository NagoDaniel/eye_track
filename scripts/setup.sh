#!/usr/bin/env bash
# Interactive setup for a fresh server (PLAN §11.3).
#
# What it does, in order:
#   1. checks Docker and Docker Compose versions and that the Docker daemon is reachable;
#   2. creates .env from .env.example if missing, asks for DOMAIN / TLS mode, and replaces the
#      "change-me" secrets with random values (values that are already set are never changed);
#   3. validates the configuration, and for TLS_MODE=acme checks that DOMAIN resolves to this server;
#   4. builds and starts the stack and waits until every healthcheck passes;
#   5. creates the first admin account.
# It is safe to run again: secrets and existing admins are kept, the stack is updated in place.
#
# Usage: scripts/setup.sh [options]
#   --non-interactive          never prompt; everything comes from .env and flags
#   --env-file FILE            use FILE instead of ./.env
#   --env-only                 only create/complete the env file (generate secrets), then exit;
#                              needs no Docker. Handy before `make dev`.
#   --dry-run                  run all checks and prepare the env file, but do not start anything
#   --skip-dns-check           do not check that DOMAIN points at this server (TLS_MODE=acme)
#   --admin-username NAME      admin account to create (default: admin)
#   --admin-password-stdin     read the admin password from the first line of stdin
#   --no-admin                 do not create an admin account
#   --project NAME             Docker Compose project name (default: eyetrack)
#   -h, --help                 show this help
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Minimum versions (named config, overridable for tests).
MIN_DOCKER="${SETUP_MIN_DOCKER:-24.0}"
MIN_COMPOSE="${SETUP_MIN_COMPOSE:-2.24}"
WAIT_TIMEOUT_S="${SETUP_WAIT_TIMEOUT_S:-900}"
PLACEHOLDER="change-me"
EXAMPLE_DOMAIN="eyetrack.example.org"
EXAMPLE_EMAIL="you@example.org"

ENV_FILE="$ROOT/.env"
PROJECT="${COMPOSE_PROJECT_NAME:-eyetrack}"
INTERACTIVE=1
ENV_ONLY=0
DRY_RUN=0
SKIP_DNS=0
CREATE_ADMIN=1
ADMIN_USER="admin"
ADMIN_PW_STDIN=0

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*" >&2; }
warn() { echo "warning: $*" >&2; }

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
  case "$1" in
    --non-interactive) INTERACTIVE=0 ;;
    --env-file) [ $# -ge 2 ] || die "--env-file needs a value"; ENV_FILE="$2"; shift ;;
    --env-only) ENV_ONLY=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --skip-dns-check) SKIP_DNS=1 ;;
    --admin-username) [ $# -ge 2 ] || die "--admin-username needs a value"; ADMIN_USER="$2"; shift ;;
    --admin-password-stdin) ADMIN_PW_STDIN=1 ;;
    --no-admin) CREATE_ADMIN=0 ;;
    --project) [ $# -ge 2 ] || die "--project needs a value"; PROJECT="$2"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

if [ "$INTERACTIVE" = 0 ] && [ "$CREATE_ADMIN" = 1 ] && [ "$ADMIN_PW_STDIN" = 0 ] \
   && [ "$ENV_ONLY" = 0 ]; then
  die "--non-interactive needs --admin-password-stdin or --no-admin"
fi

# ---------------------------------------------------------------- helpers

# version_ge A B: true if version A >= version B (numeric, dot separated).
version_ge() {
  awk -v a="$1" -v b="$2" 'BEGIN {
    na = split(a, x, "."); nb = split(b, y, ".");
    n = (na > nb) ? na : nb;
    for (i = 1; i <= n; i++) {
      xi = (i <= na) ? x[i] + 0 : 0; yi = (i <= nb) ? y[i] + 0 : 0;
      if (xi > yi) exit 0; if (xi < yi) exit 1;
    }
    exit 0 }'
}

get_env() { grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- || true; }

# set_env KEY VALUE: replace the line (or append). Uses ENVIRON so no escape processing happens.
set_env() {
  local tmp
  tmp="$(mktemp)"
  KEY="$1" VAL="$2" awk '
    BEGIN { FS = OFS = "=" }
    $1 == ENVIRON["KEY"] { print ENVIRON["KEY"] "=" ENVIRON["VAL"]; found = 1; next }
    { print }
    END { if (!found) print ENVIRON["KEY"] "=" ENVIRON["VAL"] }' "$ENV_FILE" > "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}

random_secret() { head -c 64 /dev/urandom | base64 | tr -d '\n=+/' | cut -c1-40; }

# ask VAR PROMPT: prompt with the current value as default (interactive only).
ask() {
  local var=$1 prompt=$2 current answer
  current="$(get_env "$var")"
  [ "$INTERACTIVE" = 1 ] || return 0
  printf '%s [%s]: ' "$prompt" "$current" >&2
  IFS= read -r answer || answer=""
  [ -z "$answer" ] || set_env "$var" "$answer"
}

compose() {
  (cd "$ROOT" && docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f docker-compose.yml "$@")
}

# ------------------------------------------------- 1. Docker and Compose

if [ "$ENV_ONLY" = 0 ]; then
  info "Checking Docker and Docker Compose"
  command -v docker >/dev/null 2>&1 || die "docker is not installed (need Docker Engine $MIN_DOCKER or newer)"
  docker_version="$(docker version --format '{{.Server.Version}}' 2>/dev/null)" \
    || die "cannot reach the Docker daemon (is it running, and are you in the 'docker' group?)"
  version_ge "$docker_version" "$MIN_DOCKER" \
    || die "Docker $MIN_DOCKER or newer required, found $docker_version"
  compose_version="$(docker compose version --short 2>/dev/null)" \
    || die "the Docker Compose plugin is missing (need $MIN_COMPOSE or newer)"
  compose_version="${compose_version#v}"
  version_ge "$compose_version" "$MIN_COMPOSE" \
    || die "Docker Compose $MIN_COMPOSE or newer required, found $compose_version"
  echo "    Docker $docker_version, Compose $compose_version" >&2
fi

# ------------------------------------------------------ 2. env file

if [ ! -f "$ENV_FILE" ]; then
  info "Creating $ENV_FILE from .env.example"
  cp "$ROOT/.env.example" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

if [ "$ENV_ONLY" = 0 ]; then
  ask DOMAIN "Domain participants will use"
  ask TLS_MODE "TLS mode (acme = public domain, files = own certificate, internal = lab network)"
  case "$(get_env TLS_MODE)" in
    acme) ask ACME_EMAIL "Contact e-mail for certificate expiry notices (optional)" ;;
    files)
      ask TLS_CERT_FILE "Path of the certificate file on this server"
      ask TLS_KEY_FILE "Path of the private key file on this server"
      ;;
  esac
fi

# Never change a value that is already set; only replace the placeholder.
for key in POSTGRES_PASSWORD SECRET_KEY; do
  if [ "$(get_env "$key")" = "$PLACEHOLDER" ] || [ -z "$(get_env "$key")" ]; then
    if [ "$key" = POSTGRES_PASSWORD ] && [ "$ENV_ONLY" = 0 ] \
       && docker volume inspect "${PROJECT}_pgdata" >/dev/null 2>&1; then
      die "a database volume (${PROJECT}_pgdata) already exists but POSTGRES_PASSWORD is not set in $ENV_FILE.
       The database keeps the password it was created with, so a new random one would lock the
       system out. Restore the original password in $ENV_FILE, or, if there is no data worth keeping,
       remove the volume with: docker compose -p $PROJECT down -v"
    fi
    set_env "$key" "$(random_secret)"
    info "Generated a random $key"
  fi
done

if [ "$ENV_ONLY" = 1 ]; then
  info "Environment file ready: $ENV_FILE"
  exit 0
fi

# ------------------------------------------------------ 3. validation

info "Checking configuration"
domain="$(get_env DOMAIN)"
tls_mode="$(get_env TLS_MODE)"
[ -n "$domain" ] || die "DOMAIN is empty in $ENV_FILE"
[ "$domain" != "$EXAMPLE_DOMAIN" ] || die "DOMAIN is still the example value '$EXAMPLE_DOMAIN'; set your own in $ENV_FILE"
printf '%s' "$domain" | grep -qE '^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$' \
  || die "DOMAIN '$domain' is not a valid host name"
case "$tls_mode" in
  acme)
    # The contact e-mail is optional (expiry reminders only); the example value counts as unset.
    email="$(get_env ACME_EMAIL)"
    if [ -z "$email" ] || [ "$email" = "$EXAMPLE_EMAIL" ]; then
      echo "    ACME_EMAIL not set: the certificate is requested without a contact address" >&2
    else
      printf '%s' "$email" | grep -Eq '^[^[:space:]@{}"]+@[^[:space:]@{}"]+$' \
        || die "ACME_EMAIL '$email' is not a valid e-mail address"
    fi
    ;;
  files)
    for key in TLS_CERT_FILE TLS_KEY_FILE; do
      path="$(get_env "$key")"
      [ -n "$path" ] || die "TLS_MODE=files needs $key in $ENV_FILE"
      [ -s "$path" ] || die "$key does not point at a readable, non-empty file: $path"
    done
    ;;
  internal) ;;
  *) die "TLS_MODE must be one of: acme, files, internal (got '${tls_mode}')" ;;
esac

resolve_ipv4() {
  if command -v getent >/dev/null 2>&1; then
    getent ahostsv4 "$1" 2>/dev/null | awk '{print $1}' | sort -u
  elif command -v dscacheutil >/dev/null 2>&1; then
    dscacheutil -q host -a name "$1" 2>/dev/null | awk '/^ip_address:/{print $2}' | sort -u
  elif command -v dig >/dev/null 2>&1; then
    dig +short A "$1" | grep -E '^[0-9.]+$' | sort -u
  else
    return 0
  fi
}

local_ipv4() {
  if command -v ip >/dev/null 2>&1; then
    ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1
  else
    ifconfig 2>/dev/null | awk '/inet /{print $2}'
  fi
}

if [ "$tls_mode" = acme ] && [ "$SKIP_DNS" = 0 ]; then
  info "Checking that $domain points at this server"
  resolved="$(resolve_ipv4 "$domain")"
  [ -n "$resolved" ] || die "$domain does not resolve. Create the DNS A record first, or use --skip-dns-check.
       Let's Encrypt cannot issue a certificate until it resolves to this server."
  here="$(local_ipv4)"
  match=0
  for ip in $resolved; do
    printf '%s\n' "$here" | grep -qxF "$ip" && match=1
  done
  if [ "$match" = 0 ]; then
    msg="$domain resolves to $(echo "$resolved" | tr '\n' ' ')but this server has $(echo "$here" | tr '\n' ' ').
       If this server is behind NAT or a load balancer that is expected; otherwise fix the DNS record."
    if [ "$INTERACTIVE" = 1 ]; then
      warn "$msg"
      printf 'Continue anyway? [y/N]: ' >&2
      IFS= read -r answer || answer=""
      case "$answer" in y|Y|yes|YES) ;; *) die "aborted" ;; esac
    else
      die "$msg
       Use --skip-dns-check to continue anyway."
    fi
  fi
fi

if [ "$DRY_RUN" = 1 ]; then
  info "Dry run: configuration is valid; nothing was started"
  exit 0
fi

# ------------------------------------------------------ 4. start

info "Building and starting the stack (this can take several minutes the first time)"
if ! compose up -d --build --wait --wait-timeout "$WAIT_TIMEOUT_S"; then
  compose ps -a >&2 || true
  compose logs --tail 40 >&2 || true
  die "the stack did not become healthy; see the logs above"
fi
compose ps >&2

# ------------------------------------------------------ 5. admin

if [ "$CREATE_ADMIN" = 1 ]; then
  password=""
  if [ "$ADMIN_PW_STDIN" = 1 ]; then
    IFS= read -r password || true
    [ -n "$password" ] || die "--admin-password-stdin: no password on stdin"
  else
    printf 'Admin username [%s]: ' "$ADMIN_USER" >&2
    IFS= read -r answer || answer=""
    [ -z "$answer" ] || ADMIN_USER="$answer"
    for _ in 1 2 3; do
      printf 'Admin password (min. 12 characters): ' >&2
      IFS= read -rs password || password=""; echo >&2
      printf 'Repeat password: ' >&2
      IFS= read -rs again || again=""; echo >&2
      [ "$password" = "$again" ] && [ -n "$password" ] && break
      warn "passwords differ, try again"; password=""
    done
    [ -n "$password" ] || die "no matching admin password entered"
  fi
  info "Creating admin '$ADMIN_USER'"
  if out="$(printf '%s\n' "$password" | compose exec -T api python cli.py create-admin \
        --username "$ADMIN_USER" --password-stdin 2>&1)"; then
    echo "    $out" >&2
  elif printf '%s' "$out" | grep -q "already exists"; then
    echo "    admin '$ADMIN_USER' already exists; left unchanged" >&2
  else
    die "could not create the admin: $out"
  fi
fi

info "Done"
scheme="https"
port="$(get_env HTTPS_PORT)"
url="$scheme://$domain"
{ [ -z "$port" ] || [ "$port" = 443 ]; } || url="$url:$port"
echo "    Admin login: $url/admin/login" >&2
[ "$tls_mode" != internal ] || echo "    TLS_MODE=internal: browsers must trust Caddy's local CA (see README)." >&2
