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

echo "== fake camera clip (the committed face clip as .y4m, made with the ffmpeg in the backend image)"
cp e2e/fixtures/face.mp4 "$work/face.mp4"
docker run --rm -u "$(id -u):$(id -g)" -v "$work:/w" --entrypoint ffmpeg eyetrack-backend:local \
  -loglevel error -i /w/face.mp4 -pix_fmt yuv420p /w/face.y4m || fail "could not create the fake camera clip"

echo "== browser tests"
# Chained with && on purpose: `set -e` is ignored inside a subshell that is followed by `||`.
install_browser="npx playwright install chromium"
[ -z "${CI:-}" ] || install_browser="npx playwright install --with-deps chromium"
( cd e2e && npm ci >/dev/null && $install_browser >/dev/null \
  && E2E_BASE_URL="https://localhost:$https_port" E2E_ADMIN_USER=e2e E2E_ADMIN_PASSWORD="$admin_password" \
     E2E_VIDEO="$work/clip.mp4" E2E_FACE_Y4M="$work/face.y4m" npx playwright test ) \
  || { compose logs --tail 60 api proxy >&2; fail "playwright tests failed"; }

echo "== raw data on the server is complete (PLAN §14, milestone 2)"
python3 - "$project" "$envf" <<'PY'
import json, subprocess, sys
project, envf = sys.argv[1:3]
def run(*args: str) -> str:
    return subprocess.run(
        ["docker", "compose", "-p", project, "--env-file", envf, "-f", "docker-compose.yml", *args],
        check=True, capture_output=True, text=True).stdout
def sh(script: str) -> str:
    return run("exec", "-T", "api", "sh", "-c", script)

dirs = sh("ls -d /data/studies/*/sessions/*/raw").split()
assert len(dirs) == 5, dirs
recorded = 0
for raw in dirs:
    meta = json.loads(sh(f"cat {raw}/meta.json"))
    assert meta["status"] == "uploaded" and meta["config"]["playlist"], (raw, meta)
    events = [json.loads(l) for l in sh(f"cat {raw}/events.jsonl").splitlines()]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "session_start", (raw, kinds[:5])
    if "recorder_stop" not in kinds:
        continue  # the session that was reloaded early never recorded
    recorded += 1
    features = [json.loads(l) for l in sh(f"cat {raw}/browser_features.jsonl").splitlines()]
    frames = [json.loads(l) for l in sh(f"cat {raw}/stimulus_frames.jsonl").splitlines()]
    with_face = sum(1 for f in features if f["face"])
    assert len(features) > 100, f"{raw}: only {len(features)} feature records"
    assert with_face > 0.9 * len(features), f"{raw}: face found in {with_face}/{len(features)} frames"
    assert all(len(f["lm"]) == 49 and len(f["matrix"]) == 16 for f in features if f["face"])
    assert len(frames) > 40, f"{raw}: only {len(frames)} stimulus frames"
    assert [f["t"] for f in features] == sorted(f["t"] for f in features)
    assert [f["t"] for f in frames] == sorted(f["t"] for f in frames)
    # The chunks of one recording are one WebM byte stream: concatenated they are a valid video.
    probe = json.loads(sh(
        f"cat {raw}/chunks/webcam/*.bin > /tmp/webcam.webm && "
        "ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height "
        "-of json /tmp/webcam.webm"))["streams"][0]
    assert probe["codec_name"] in ("vp8", "vp9") and probe["width"] >= 640, probe
    last_pts = float(sh("ffprobe -v error -show_entries packet=pts_time -of csv=p=0 "
                        "/tmp/webcam.webm | tail -1").strip())
    span = (features[-1]["t"] - features[0]["t"]) / 1000
    assert abs(last_pts - span) < 3, f"{raw}: webcam lasts {last_pts:.1f} s, features span {span:.1f} s"
    print(f"{raw.split('/')[-2][:8]}: {len(features)} feature records ({with_face} with a face), "
          f"{len(frames)} stimulus frames, webcam {probe['codec_name']} {probe['width']}x{probe['height']} "
          f"{last_pts:.1f} s (features span {span:.1f} s)")
assert recorded == 4, recorded
print(f"{len(dirs)} sessions, {recorded} with a complete recording")
PY
echo "ALL E2E CHECKS PASSED"
