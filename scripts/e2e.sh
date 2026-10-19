#!/usr/bin/env bash
# End-to-end test (PLAN §13, §14): real Chromium (Playwright) against the real production stack with
# TLS_MODE=internal. Uses a temporary env file, data directory and compose project; leaves `.env`
# and `data/` alone. Run via `make e2e`. Needs Docker, Node 20 and network access for the
# Playwright browser download.
set -euo pipefail

cd "$(dirname "$0")/.."
# Recorded in each analysis run (PLAN §9); empty outside a git checkout.
GIT_SHA="$(git describe --always --dirty --abbrev=40 2>/dev/null || true)"
export GIT_SHA
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
import json, os, subprocess, sys
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
complete = []  # recorded sessions that played the video (candidates for E2E_EXPORT_SESSION)
for raw in dirs:
    meta = json.loads(sh(f"cat {raw}/meta.json"))
    assert meta["status"] == "uploaded" and meta["config"]["playlist"], (raw, meta)
    events = [json.loads(l) for l in sh(f"cat {raw}/events.jsonl").splitlines()]
    kinds = [e["type"] for e in events]
    assert kinds[0] == "session_start", (raw, kinds[:5])
    if "recorder_stop" not in kinds:
        continue  # the session that was reloaded early never recorded
    recorded += 1
    if "video_ended" in kinds and kinds.count("calib_gate_result") == 2:
        complete.append(raw)
    features = [json.loads(l) for l in sh(f"cat {raw}/browser_features.jsonl").splitlines()]
    frames = [json.loads(l) for l in sh(f"cat {raw}/stimulus_frames.jsonl").splitlines()]
    with_face = sum(1 for f in features if f["face"])
    # A slow CI machine runs the model at a lower rate, so the count is compared with the duration.
    seconds = (features[-1]["t"] - features[0]["t"]) / 1000
    assert len(features) >= max(20, 3 * seconds), f"{raw}: only {len(features)} feature records in {seconds:.1f} s"
    assert with_face > 0.9 * len(features), f"{raw}: face found in {with_face}/{len(features)} frames"
    assert all(len(f["lm"]) == 49 and len(f["matrix"]) == 16 for f in features if f["face"])
    # The browser's own MediaPipe output is read correctly (the fixtures come from the Python API):
    # the matrix is row-major (bottom row 0 0 0 1, translation in cm in indices 3, 7, 11), the head
    # of someone looking at the screen is roughly frontal, and the measurements are plausible.
    quality = meta["config"]["quality"]
    for f in (f for f in features if f["face"]):
        m = f["matrix"]
        assert all(abs(v) < 1e-3 for v in m[12:15]) and abs(m[15] - 1) < 1e-3, f"matrix layout: {m}"
        assert -100 < m[11] < -10, f"distance (cm) {m[11]}"
        assert abs(m[3]) < 30 and abs(m[7]) < 30, f"translation {m[3]}, {m[7]}"
        assert abs(m[8]) < 0.5 and abs(m[9]) < 0.5, "yaw or pitch over 30 degrees on a frontal clip"
        assert quality["face_ratio_min"] <= f["face_ratio"] <= quality["face_ratio_max"], f["face_ratio"]
        assert f["brightness"] is not None and 0 < f["brightness"] < 1, f["brightness"]
        assert 0 <= f["blink_l"] <= 1 and 0 <= f["blink_r"] <= 1
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

# The worker processes every completed session (PLAN §8, §13): wait for the queue to empty, then
# check the derived files of each session that recorded the webcam.
import time
def open_jobs() -> int:
    query = "select count(*) from jobs where status in ('queued', 'running')"
    return int(run("exec", "-T", "db", "psql", "-U", "eyetrack", "-d", "eyetrack", "-At", "-c", query))
deadline = time.time() + 300
while open_jobs() and time.time() < deadline:
    time.sleep(2)
assert open_jobs() == 0, "the worker did not finish the processing jobs in 300 s"
def count(where: str) -> int:
    return int(run("exec", "-T", "db", "psql", "-U", "eyetrack", "-d", "eyetrack", "-At", "-c",
                   f"select count(*) from jobs where {where}"))
failed = count("status = 'failed'")
# Only the session that was reloaded before it recorded anything fails (it has no webcam).
no_webcam = count("status = 'failed' and log like '%webcam: there are no chunks%'")
assert failed == no_webcam == 1, f"{failed} failed jobs, {no_webcam} of them for a missing webcam"
checker = open("scripts/e2e_check_derived.py").read()
for raw in dirs:
    events = [json.loads(l) for l in sh(f"cat {raw}/events.jsonl").splitlines()]
    if not any(e["type"] == "recorder_stop" for e in events):
        continue
    out = subprocess.run(
        ["docker", "compose", "-p", project, "--env-file", envf, "-f", "docker-compose.yml",
         "exec", "-T", "api", "python", "-", raw.rsplit("/", 1)[0] + "/derived"],
        input=checker, check=True, capture_output=True, text=True).stdout.strip()
    print(out)
print("the session without a webcam failed to process, as expected")

# E2E_EXPORT_SESSION=<dir>: copy the raw directory of one complete session and the server's chunk
# manifest out of the stack, for scripts/make_session_fixture.py (the pipeline tests' real session).
export = os.environ.get("E2E_EXPORT_SESSION")
if export:
    assert complete, "no session played the video and logged both gate results"
    raw = complete[0]
    session_id = raw.split("/")[-2]
    os.makedirs(export, exist_ok=True)
    tar = subprocess.run(
        ["docker", "compose", "-p", project, "--env-file", envf, "-f", "docker-compose.yml",
         "exec", "-T", "api", "tar", "-C", raw, "-cf", "-", "."],
        check=True, capture_output=True).stdout
    subprocess.run(["tar", "-C", export, "-xf", "-"], input=tar, check=True)
    query = ("select coalesce(json_agg(json_build_object('stream', stream, 'seq', seq, "
             "'size', size, 'sha256', sha256) order by stream, seq), '[]') from chunks "
             f"where session_id = '{session_id}'")
    manifest = run("exec", "-T", "db", "psql", "-U", "eyetrack", "-d", "eyetrack", "-At", "-c", query)
    with open(os.path.join(export, "chunks.json"), "w") as f:
        f.write(manifest.strip() + "\n")
    print(f"exported session {session_id} to {export}")
PY
echo "ALL E2E CHECKS PASSED"
