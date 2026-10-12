#!/usr/bin/env bash
# Builds the backend image and checks it (PLAN §11.1). Run via `make test-images`.
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE="eyetrack-backend:test"
fail() { echo "FAIL: $*" >&2; exit 1; }
run() { docker run --rm --entrypoint "" "$IMAGE" "$@"; }

echo "== build"
docker build -t "$IMAGE" backend >/dev/null

echo "== runs as non-root"
uid=$(run id -u)
[ "$uid" != "0" ] || fail "container runs as root"

echo "== ffmpeg present"
run ffmpeg -version | head -1

echo "== python deps import (real mediapipe, opencv, av)"
run python -c "import mediapipe, cv2, av; print('mediapipe', mediapipe.__version__, 'cv2', cv2.__version__, 'av', av.__version__)"

echo "== face_landmarker.task present, hash matches, and loads in real MediaPipe"
expected=$(grep -oE 'FACE_LANDMARKER_SHA256=[0-9a-f]{64}' backend/Dockerfile | cut -d= -f2)
actual=$(run sha256sum /opt/mediapipe/face_landmarker.task | cut -d' ' -f1)
[ "$expected" = "$actual" ] || fail "model hash mismatch: $actual"
run python -c "
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import FaceLandmarker, FaceLandmarkerOptions
opts = FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path='/opt/mediapipe/face_landmarker.task'))
FaceLandmarker.create_from_options(opts).close()
print('FaceLandmarker loaded')
"

echo "== CLI and alembic available"
run python cli.py --help >/dev/null
run alembic --help >/dev/null

echo "== wrong sha256 fails the build"
bad_hash=$(printf '0%.0s' {1..64})
if out=$(docker build --progress=plain --build-arg FACE_LANDMARKER_SHA256="$bad_hash" \
  -t eyetrack-backend:badhash backend 2>&1); then
  fail "build succeeded with a wrong model sha256"
fi
echo "$out" | grep -qi "checksum" || fail "build failed, but not because of the checksum"
echo "build rejected a wrong hash (expected)"

echo "== api boots and logs JSON (no database: health must be 503)"
cid=$(docker run -d --rm -e SECRET_KEY=image-test-secret -e POSTGRES_PASSWORD=x \
  -e POSTGRES_HOST=127.0.0.1 -e API_WORKERS=1 "$IMAGE")
trap 'docker stop "$cid" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 30); do
  docker logs "$cid" 2>&1 | grep -q "Application startup complete" && break
  sleep 1
done
status=$(docker exec "$cid" python -c "
import urllib.request, urllib.error
try:
    urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=10)
    print(200)
except urllib.error.HTTPError as e:
    print(e.code)
")
[ "$status" = "503" ] || fail "expected 503 without a database, got $status"
docker logs "$cid" 2>&1 | python3 -c "
import json, sys
lines = [l for l in sys.stdin.read().splitlines() if l.strip()]
bad = [l for l in lines if not l.startswith('{') or json.loads(l) is None]
assert lines and not bad, 'non-JSON log lines: %r' % bad[:3]
print(len(lines), 'log lines, all JSON')
"

echo "ALL IMAGE CHECKS PASSED"
