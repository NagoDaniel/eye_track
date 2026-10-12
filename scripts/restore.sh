#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh into a running stack (PLAN §11.4).
#
# Usage: scripts/restore.sh [options] BACKUP_DIR
#   --yes                skip the confirmation prompt
#   --force              restore even if the target already holds studies, videos, sessions or files
#                        (the database content is replaced; files with the same name are overwritten,
#                        other files are kept)
#   --env-file FILE      use FILE instead of ./.env
#   --project NAME       Docker Compose project name (default: eyetrack)
#   -h, --help           show this help
#
# Typical use on a new server: run scripts/setup.sh first (empty stack), then this script.
# Checks the sha256 of both files before touching anything, restores the database in one
# transaction, extracts the data, restarts the stack (migrations run if the backup is older than
# the code) and compares row counts with the manifest.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
PROJECT="${COMPOSE_PROJECT_NAME:-eyetrack}"
ASSUME_YES=0
FORCE=0
BACKUP=""

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --yes) ASSUME_YES=1 ;;
    --force) FORCE=1 ;;
    --env-file) [ $# -ge 2 ] || die "--env-file needs a value"; ENV_FILE="$2"; shift ;;
    --project) [ $# -ge 2 ] || die "--project needs a value"; PROJECT="$2"; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *) [ -z "$BACKUP" ] || die "only one BACKUP_DIR is allowed"; BACKUP="$1" ;;
  esac
  shift
done
[ -n "$BACKUP" ] || die "BACKUP_DIR is required (see --help)"
[ -d "$BACKUP" ] || die "not a directory: $BACKUP"
[ -f "$ENV_FILE" ] || die "env file not found: $ENV_FILE"
BACKUP="$(cd "$BACKUP" && pwd)"

compose() {
  (cd "$ROOT" && docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f docker-compose.yml "$@")
}
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}
sql() { compose exec -T db psql -U eyetrack -d eyetrack -Atc "$1"; }
manifest() { grep -E "^$1: " "$BACKUP/MANIFEST.txt" | head -1 | cut -d' ' -f2-; }

info "Checking the backup"
for f in MANIFEST.txt db.dump data.tar.gz; do
  [ -f "$BACKUP/$f" ] || die "$BACKUP/$f is missing; this is not a complete backup"
done
[ "$(manifest format)" = 1 ] || die "unsupported backup format '$(manifest format)'"
for f in db.dump data.tar.gz; do
  [ "$(sha256_of "$BACKUP/$f")" = "$(manifest "sha256_$f")" ] \
    || die "checksum mismatch for $f: the backup is corrupt or was modified; nothing was changed"
done
gzip -t "$BACKUP/data.tar.gz" || die "data.tar.gz is corrupt; nothing was changed"
echo "    created $(manifest created), raw video $(manifest raw_video), $(manifest sessions) sessions, $(manifest data_files) files" >&2

[ -n "$(compose ps --status running -q db 2>/dev/null)" ] \
  || die "the 'db' service is not running; run scripts/setup.sh (or docker compose up -d) first"

if [ "$FORCE" = 0 ]; then
  existing="$(sql 'SELECT (SELECT count(*) FROM studies) + (SELECT count(*) FROM videos) + (SELECT count(*) FROM sessions)')"
  [ "$existing" = 0 ] || die "the target database is not empty ($existing studies/videos/sessions); use --force to replace it"
  files="$(compose run --rm --no-deps -T api sh -c 'find /data/studies /data/videos /data/analysis -type f 2>/dev/null | head -1')"
  [ -z "$files" ] || die "the target data directory is not empty; use --force to overwrite files"
fi

if [ "$ASSUME_YES" = 0 ]; then
  echo "This replaces the database content of project '$PROJECT' and writes the backed-up files." >&2
  printf "Type the project name (%s) to continue: " "$PROJECT" >&2
  IFS= read -r answer || answer=""
  [ "$answer" = "$PROJECT" ] || die "aborted"
fi

info "Stopping api, worker and proxy"
compose stop api worker proxy >/dev/null

info "Restoring the database (single transaction)"
compose exec -T db pg_restore -U eyetrack -d eyetrack --single-transaction --clean --if-exists \
  --no-owner < "$BACKUP/db.dump" \
  || die "pg_restore failed; the database was left unchanged (single transaction). api/worker/proxy are stopped."

info "Extracting the data directory"
compose run --rm --no-deps -T api tar -xzf - -C /data < "$BACKUP/data.tar.gz" \
  || die "extracting the data failed; the database is already restored. api/worker/proxy are stopped."

info "Starting the stack"
compose up -d --wait --wait-timeout "${RESTORE_WAIT_TIMEOUT_S:-600}" >/dev/null \
  || die "the stack did not become healthy after the restore"

info "Verifying row counts against the manifest"
for table in studies videos sessions; do
  want="$(manifest "$table")"
  got="$(sql "SELECT count(*) FROM $table")"
  [ "$got" = "$want" ] || die "$table: $got rows after the restore, the manifest says $want"
  echo "    $table: $got" >&2
done
info "Restore complete"
