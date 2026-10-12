#!/usr/bin/env bash
# Round-trip test for scripts/backup.sh and scripts/restore.sh (PLAN §11.4). Run via `make test-backup`.
# Seeds a real stack, backs it up, destroys database AND data, restores into a fresh stack and
# compares every row and every file. Uses a temporary env file, data dir and compose project.
set -euo pipefail

cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }

work=$(mktemp -d /tmp/eyetrack-test.XXXXXX)  # /tmp is shared with Docker Desktop on macOS; /var/folders is not
project="eyetrack-backup-test-$$"
envf="$work/env"
https_port=18443
rand() { python3 -c "import secrets; print(secrets.token_urlsafe(24))"; }

mkdir -p "$work/models" "$work/backups"
cat > "$envf" <<ENV
DOMAIN=localhost
TLS_MODE=internal
DATA_DIR=$work/data
MODELS_DIR=$work/models
POSTGRES_PASSWORD=$(rand)
SECRET_KEY=$(rand)
HTTP_PORT=18080
HTTPS_PORT=$https_port
ENV

compose() { docker compose -p "$project" --env-file "$envf" -f docker-compose.yml "$@"; }
backup() { ./scripts/backup.sh --env-file "$envf" --project "$project" "$@"; }
restore() { ./scripts/restore.sh --env-file "$envf" --project "$project" "$@"; }
wipe_data_dir() {
  docker run --rm -u 0 -v "$work:/w" --entrypoint rm eyetrack-backend:local -rf /w/data >/dev/null 2>&1 || true
}
cleanup() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  wipe_data_dir
  rm -rf "$work"
}
trap cleanup EXIT
bring_up() {
  compose up -d --build --wait --wait-timeout 600 >/dev/null 2>&1 || {
    compose ps -a >&2; compose logs --tail 40 >&2; fail "stack did not become healthy"
  }
}
destroy_everything() {  # database volume, caddy state and data directory
  compose down -v --remove-orphans >/dev/null 2>&1
  wipe_data_dir
}
expect_fail() {
  local pattern=$1; shift
  local out
  if out=$("$@" 2>&1); then fail "expected failure but it succeeded: $*"; fi
  printf '%s' "$out" | grep -q "$pattern" || fail "wrong error (wanted '$pattern'): $out"
}
file_fingerprint() { compose exec -T api sh -c 'cd /data && find . -type f | LC_ALL=C sort | xargs sha256sum'; }
db_fingerprint() {
  compose exec -T api python - <<'PY'
import json
from sqlalchemy import text
from app.db import make_engine
from app.settings import Settings

with make_engine(Settings().database_url).connect() as conn:
    for table in ("alembic_version", "studies", "videos", "sessions", "chunks", "admin_users"):
        for row in conn.execute(text(f"SELECT * FROM {table} ORDER BY 1")):
            print(table, json.dumps([str(v) for v in row]))
PY
}
mode_of() { stat -c '%a' "$1" 2>/dev/null || stat -f '%Lp' "$1"; }  # GNU first: on Linux `stat -f` succeeds with other output
latest_backup() { find "$work/backups" -maxdepth 1 -name 'eyetrack-backup-*' ! -name '*.partial' | LC_ALL=C sort | tail -1; }

echo "== shellcheck"
docker run --rm -v "$PWD:/mnt" koalaman/shellcheck:stable scripts/backup.sh scripts/restore.sh \
  scripts/test-backup-restore.sh || fail "shellcheck reported problems"

echo "== backup without a running stack fails loudly and leaves nothing behind"
expect_fail "is not running" backup "$work/backups"
[ -z "$(find "$work/backups" -mindepth 1)" ] || fail "failed backup left files behind"
expect_fail "TARGET_DIR is required" backup
expect_fail "unknown option" backup --bogus "$work/backups"

echo "== seed: study, video, 2 sessions with raw+derived files, chunks, admin"
bring_up
printf 'a-long-test-password\n' | compose exec -T api python cli.py create-admin --username root --password-stdin >/dev/null
compose exec -T api sh -c 'mkdir -p /data/videos/1 && ffmpeg -loglevel error -f lavfi -i testsrc=duration=2:size=160x120:rate=25 -pix_fmt yuv420p -c:v libx264 /data/videos/1/video.mp4'
compose exec -T api python - <<'PY'
import os
from pathlib import Path

from app.db import make_engine, make_session_factory
from app.models import Chunk, ChunkStream, Session, SessionStatus, Study, StudyMode, Video
from app.settings import Settings

factory = make_session_factory(make_engine(Settings().database_url))
with factory() as db:
    study = Study(name="Study", code="abc123", mode=StudyMode.REMOTE, config={"playlist": [1]})
    video = Video(title="V", file_path="videos/1/video.mp4", duration=2.0, width=160, height=120,
                  fps=25.0, sha256="0" * 64, aois={"a": 1})
    db.add_all([study, video])
    db.flush()
    for i in range(2):
        sess = Session(study_id=study.id, participant_code=f"p{i}", upload_token_hash="h",
                       status=SessionStatus.UPLOADED, flags=["interrupted"] if i else [])
        db.add(sess)
        db.flush()
        base = Path(f"/data/studies/{study.id}/sessions/{sess.id}")
        for rel, size in [("raw/webcam.webm", 300_000), ("raw/chunks/webcam/000000.bin", 1000),
                          ("raw/chunks/events/000000.bin", 500), ("raw/events.jsonl", 200),
                          ("derived/quality.json", 100)]:
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(os.urandom(size))
        db.add(Chunk(session_id=sess.id, stream=ChunkStream.WEBCAM, seq=0, size=1000, sha256="a" * 64))
    db.commit()
analysis = Path("/data/analysis/1/1/run1")
analysis.mkdir(parents=True, exist_ok=True)
(analysis / "heatmap.png").write_bytes(os.urandom(400))
PY
file_fingerprint > "$work/files.before"
db_fingerprint > "$work/db.before"
[ "$(wc -l < "$work/files.before" | tr -d ' ')" = "12" ] || fail "unexpected seed: $(wc -l < "$work/files.before") files"
grep -q '^studies' "$work/db.before" || fail "DB seed missing studies"
grep -q '^sessions' "$work/db.before" || fail "DB seed missing sessions"

echo "== full backup: files, permissions, manifest, contents"
backup "$work/backups" >/dev/null 2>"$work/b1.err" || { cat "$work/b1.err" >&2; fail "backup failed"; }
full="$(latest_backup)"
sleep 1  # distinct timestamp for the second backup
for f in db.dump data.tar.gz MANIFEST.txt; do
  [ -s "$full/$f" ] || fail "$f missing or empty"
  [ "$(mode_of "$full/$f")" = "600" ] || fail "$f is not mode 600"
done
[ "$(mode_of "$full")" = "700" ] || fail "backup directory is not mode 700"
grep -q '^raw_video: included' "$full/MANIFEST.txt" || fail "manifest does not say raw video is included"
grep -q '^sessions: 2' "$full/MANIFEST.txt" || fail "manifest session count wrong"
grep -q '^data_files: 12' "$full/MANIFEST.txt" || fail "manifest file count wrong"
[ -z "$(find "$work/backups" -name '*.partial')" ] || fail "partial directory left behind"
# Capture first: `tar | grep -q` under pipefail fails with a broken pipe once grep exits early.
full_listing="$(tar -tzf "$full/data.tar.gz")"
echo "$full_listing" | grep -q 'raw/webcam.webm' || fail "full backup lacks webcam.webm"
echo "$full_listing" | grep -q 'raw/chunks/webcam' || fail "full backup lacks webcam chunks"

echo "== --no-raw-video backup leaves out only the raw webcam files"
backup --no-raw-video "$work/backups" >/dev/null 2>&1 || fail "--no-raw-video backup failed"
slim="$(latest_backup)"
[ "$slim" != "$full" ] || fail "second backup overwrote the first"
grep -q '^raw_video: excluded' "$slim/MANIFEST.txt" || fail "manifest does not say raw video is excluded"
listing="$(tar -tzf "$slim/data.tar.gz")"
echo "$listing" | grep -q 'webcam' && fail "no-raw backup still contains webcam files"
for keep in raw/events.jsonl raw/chunks/events derived/quality.json videos/1/video.mp4 analysis/1/1/run1/heatmap.png; do
  echo "$listing" | grep -q "$keep" || fail "no-raw backup lacks $keep"
done
grep -q '^data_files: 8' "$slim/MANIFEST.txt" || fail "no-raw manifest file count wrong: $(grep data_files "$slim/MANIFEST.txt")"

echo "== restore refuses a populated target, a wrong confirmation and a missing backup"
expect_fail "not empty" restore --yes "$full"
expect_fail "aborted" bash -c "echo wrong | ./scripts/restore.sh --env-file '$envf' --project '$project' --force '$full'"
expect_fail "not a directory" restore --yes "$work/nope"
mkdir "$work/incomplete" && touch "$work/incomplete/MANIFEST.txt"
expect_fail "is missing" restore --yes "$work/incomplete"
[ "$(compose ps api --format '{{.State}}')" = "running" ] || fail "refused restores must not stop the stack"

echo "== a corrupted backup is rejected before anything is touched"
cp -R "$full" "$work/corrupt"
python3 - "$work/corrupt/data.tar.gz" <<'PY'
import sys
path = sys.argv[1]
data = bytearray(open(path, "rb").read())
data[len(data) // 2] ^= 0xFF
open(path, "wb").write(data)
PY
expect_fail "checksum mismatch" restore --yes --force "$work/corrupt"
db_fingerprint | diff -q - "$work/db.before" >/dev/null || fail "database changed by a rejected restore"
[ "$(compose ps api --format '{{.State}}')" = "running" ] || fail "rejected restore stopped the stack"

echo "== disaster: destroy database, volumes and data; restore the FULL backup into a fresh stack"
destroy_everything
bring_up
[ "$(compose exec -T db psql -U eyetrack -d eyetrack -Atc 'SELECT count(*) FROM studies')" = "0" ] || fail "fresh stack is not empty"
restore --yes "$full" >"$work/r1.log" 2>&1 || { tail -30 "$work/r1.log" >&2; fail "restore failed"; }
file_fingerprint | diff - "$work/files.before" || fail "restored files differ from the originals"
db_fingerprint | diff - "$work/db.before" || fail "restored database rows differ from the originals"
base="https://localhost:$https_port"
[ "$(curl -sk "$base/api/health")" = '{"status":"ok"}' ] || fail "stack unhealthy after the restore"
jar="$work/jar"
token=$(curl -sk -c "$jar" "$base/admin/login" | grep -o 'name="csrf_token" value="[^"]*"' | cut -d'"' -f4)
code=$(curl -sk -o /dev/null -w '%{http_code}' -b "$jar" -c "$jar" --data-urlencode "username=root" \
  --data-urlencode "password=a-long-test-password" --data-urlencode "csrf_token=$token" "$base/admin/login")
[ "$code" = "303" ] || fail "the restored admin cannot log in ($code)"
[ "$(curl -sk -o /dev/null -w '%{http_code}' -r 0-99 "$base/media/videos/1.mp4")" = "206" ] \
  || fail "restored video is not served"
echo "ok  every row and every file identical; admin login and video serving work"

echo "== --force restores over existing data and stays identical"
restore --yes --force "$full" >/dev/null 2>&1 || fail "--force restore failed"
file_fingerprint | diff -q - "$work/files.before" >/dev/null || fail "files differ after --force restore"
db_fingerprint | diff -q - "$work/db.before" >/dev/null || fail "rows differ after --force restore"

echo "== disaster again: restore the --no-raw-video backup"
destroy_everything
bring_up
restore --yes "$slim" >"$work/r2.log" 2>&1 || { tail -30 "$work/r2.log" >&2; fail "restore of the no-raw backup failed"; }
grep -v -e '/raw/webcam.webm' -e '/raw/chunks/webcam/' "$work/files.before" > "$work/files.expected"
file_fingerprint | diff - "$work/files.expected" || fail "no-raw restore has the wrong files"
db_fingerprint | diff - "$work/db.before" || fail "no-raw restore changed database rows"
echo "ok  database complete, raw webcam files absent, everything else identical"

echo "ALL BACKUP/RESTORE CHECKS PASSED"
