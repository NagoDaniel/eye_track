#!/usr/bin/env bash
# Builds the backend image and checks it (PLAN §11.1). Run via `make test-images`.
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE="eyetrack-backend:test"
fail() { echo "FAIL: $*" >&2; exit 1; }
run() { docker run --rm --entrypoint "" "$IMAGE" "$@"; }

echo "== build"
docker build -f backend/Dockerfile -t "$IMAGE" . >/dev/null

echo "== runs as non-root"
uid=$(run id -u)
[ "$uid" != "0" ] || fail "container runs as root"

echo "== ffmpeg present"
run ffmpeg -version | head -1

echo "== python deps import (real mediapipe, opencv, av)"
run python -c "import mediapipe, cv2, av; print('mediapipe', mediapipe.__version__, 'cv2', cv2.__version__, 'av', av.__version__)"

echo "== face_landmarker.task present, hash matches, and loads in real MediaPipe"
expected=$(python3 -c "import json; print(json.load(open('shared/models.json'))['face_landmarker']['sha256'])")
actual=$(run sha256sum /opt/mediapipe/face_landmarker.task | cut -d' ' -f1)
[ "$expected" = "$actual" ] || fail "model hash mismatch: $actual"
run python -c "
from mediapipe.tasks.python import BaseOptions
from mediapipe.tasks.python.vision import FaceLandmarker, FaceLandmarkerOptions
opts = FaceLandmarkerOptions(base_options=BaseOptions(model_asset_path='/opt/mediapipe/face_landmarker.task'))
FaceLandmarker.create_from_options(opts).close()
print('FaceLandmarker loaded')
"

echo "== Pipeline A features find the shared landmark subset inside the image"
run python -c "
from worker.pipeline.features_a import LANDMARK_COUNT
assert LANDMARK_COUNT == 49, LANDMARK_COUNT
print('landmark subset:', LANDMARK_COUNT)
"

echo "== the moving-dot ground truth is in the image (cli.py moving-dot-report)"
run python -c "
from pathlib import Path
from worker.pipeline.moving_dot import DotPath
path = DotPath.load(Path('/shared/moving_dot_path.json'))
print('moving-dot path:', path.duration_s, 's')
"

echo "== CLI and alembic available"
run python cli.py --help >/dev/null
# The CLI reads its settings before it shows the help of a command, so give it dummy ones.
for command in process export-session import-session moving-dot-report; do
  docker run --rm --entrypoint "" -e SECRET_KEY=image-test-secret-0123456789abcdef \
    -e POSTGRES_PASSWORD=x "$IMAGE" python cli.py "$command" --help >/dev/null \
    || fail "cli.py $command --help failed"
done
run alembic --help >/dev/null

echo "== wrong sha256 in the manifest fails the build"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/shared" "$tmp/e2e/fixtures"
cp -R backend "$tmp/backend"
cp shared/download_model.py shared/landmark_subset.json "$tmp/shared/"
cp e2e/fixtures/moving_dot_path.json "$tmp/e2e/fixtures/"
python3 -c "
import json
m = json.load(open('shared/models.json'))
m['face_landmarker']['sha256'] = '0' * 64
json.dump(m, open('$tmp/shared/models.json', 'w'))
"
cp backend/Dockerfile.dockerignore "$tmp/backend/Dockerfile.dockerignore"
if out=$(docker build --progress=plain -f "$tmp/backend/Dockerfile" -t eyetrack-backend:badhash "$tmp" 2>&1); then
  fail "build succeeded with a wrong model sha256"
fi
echo "$out" | grep -q "sha256 mismatch" || fail "build failed, but not because of the sha256 check"
echo "build rejected a wrong hash (expected)"

echo "== api boots and logs JSON (no database: health must be 503)"
cid=$(docker run -d --rm -e SECRET_KEY=image-test-secret-0123456789abcdef -e POSTGRES_PASSWORD=x \
  -e POSTGRES_HOST=127.0.0.1 -e API_WORKERS=1 "$IMAGE")
trap 'docker stop "$cid" >/dev/null 2>&1 || true; rm -rf "$tmp"' EXIT
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
