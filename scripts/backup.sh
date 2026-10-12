#!/usr/bin/env bash
# Back up the database and the data directory of a running stack (PLAN §11.4).
#
# Usage: scripts/backup.sh [options] TARGET_DIR
#   --no-raw-video       leave out raw webcam files (webcam.webm and chunks/webcam) of every session
#   --env-file FILE      use FILE instead of ./.env
#   --project NAME       Docker Compose project name (default: eyetrack)
#   -h, --help           show this help
#
# Creates TARGET_DIR/eyetrack-backup-<timestamp>/ with
#   db.dump         pg_dump (custom format)
#   data.tar.gz     everything under DATA_DIR: videos, raw and derived session data, analyses
#   MANIFEST.txt    options, row/file counts and sha256 of the two files (checked by restore.sh)
# The stack must be running (data is read through the api container). The backup contains
# participant recordings: store it on an encrypted volume. `.env` (secrets) is NOT included.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
PROJECT="${COMPOSE_PROJECT_NAME:-eyetrack}"
RAW_VIDEO=included
TARGET=""

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --no-raw-video) RAW_VIDEO=excluded ;;
    --env-file) [ $# -ge 2 ] || die "--env-file needs a value"; ENV_FILE="$2"; shift ;;
    --project) [ $# -ge 2 ] || die "--project needs a value"; PROJECT="$2"; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *) [ -z "$TARGET" ] || die "only one TARGET_DIR is allowed"; TARGET="$1" ;;
  esac
  shift
done
[ -n "$TARGET" ] || die "TARGET_DIR is required (see --help)"
[ -f "$ENV_FILE" ] || die "env file not found: $ENV_FILE"

compose() {
  (cd "$ROOT" && docker compose -p "$PROJECT" --env-file "$ENV_FILE" -f docker-compose.yml "$@")
}
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}
sql() { compose exec -T db psql -U eyetrack -d eyetrack -Atc "$1"; }

for svc in db api; do
  [ -n "$(compose ps --status running -q "$svc" 2>/dev/null)" ] \
    || die "the '$svc' service is not running; start the stack first (docker compose up -d)"
done

mkdir -p "$TARGET"
stamp="$(date +%Y%m%d-%H%M%S)"
final="$TARGET/eyetrack-backup-$stamp"
[ ! -e "$final" ] || die "$final already exists"
partial="$final.partial"
trap 'rm -rf "$partial"' EXIT
mkdir -m 700 "$partial"

info "Dumping the database"
compose exec -T db pg_dump -U eyetrack -d eyetrack --format=custom --no-owner > "$partial/db.dump"
[ -s "$partial/db.dump" ] || die "pg_dump produced an empty file"

info "Archiving the data directory (raw webcam video: $RAW_VIDEO)"
excludes=()
if [ "$RAW_VIDEO" = excluded ]; then
  excludes=(--exclude='*/raw/webcam.webm' --exclude='*/raw/chunks/webcam')
fi
rc=0
# ${arr[@]+...}: an empty array must not trip `set -u` on bash 3.2 (macOS).
compose exec -T api tar -czf - -C /data ${excludes[@]+"${excludes[@]}"} . > "$partial/data.tar.gz" || rc=$?
# tar exits 1 when a file changed while being read (e.g. an upload in progress): keep the backup.
gzip -t "$partial/data.tar.gz" 2>/dev/null || die "archiving the data directory failed (exit code $rc): the archive is missing or corrupt"
[ -n "$(gzip -dc "$partial/data.tar.gz" | tar -tf - | head -1)" ] || die "the archive is empty; refusing to continue (exit code $rc)"
if [ "$rc" = 1 ]; then
  echo "warning: some files changed while being archived; take the backup when no study is running for a consistent copy" >&2
elif [ "$rc" != 0 ]; then
  die "archiving the data directory failed (exit code $rc)"
fi

files="$(gzip -dc "$partial/data.tar.gz" | tar -tf - | grep -vc '/$' || true)"
{
  echo "format: 1"
  echo "created: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "project: $PROJECT"
  echo "raw_video: $RAW_VIDEO"
  echo "git: $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  echo "studies: $(sql 'SELECT count(*) FROM studies')"
  echo "videos: $(sql 'SELECT count(*) FROM videos')"
  echo "sessions: $(sql 'SELECT count(*) FROM sessions')"
  echo "data_files: $files"
  echo "sha256_db.dump: $(sha256_of "$partial/db.dump")"
  echo "sha256_data.tar.gz: $(sha256_of "$partial/data.tar.gz")"
} > "$partial/MANIFEST.txt"
chmod 600 "$partial"/*

mv "$partial" "$final"
trap - EXIT
info "Backup written to $final"
du -sh "$final"/* >&2
echo "$final"
