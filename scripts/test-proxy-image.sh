#!/usr/bin/env bash
# Builds the proxy image and checks it against a real backend container (PLAN §11.1, §11.2).
# Needs the backend image `eyetrack-backend:test` (built here if missing). Run via `make test-images`.
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE="eyetrack-proxy:test"
API_IMAGE="eyetrack-backend:test"
fail() { echo "FAIL: $*" >&2; exit 1; }
sha256() { sha256sum "$@" 2>/dev/null || shasum -a 256 "$@"; }  # GNU first, macOS fallback

work=$(mktemp -d /tmp/eyetrack-test.XXXXXX)  # /tmp is shared with Docker Desktop on macOS; /var/folders is not
net="eyetrack-proxytest-$$"
cleanup() {
  docker rm -f "${net}-api" "${net}-proxy" "${net}-stub" "${net}-proxy2" >/dev/null 2>&1 || true
  docker network rm "$net" "${net}-b" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT

echo "== build"
docker build -f proxy/Dockerfile -t "$IMAGE" . >/dev/null
docker image inspect "$API_IMAGE" >/dev/null 2>&1 || docker build -f backend/Dockerfile -t "$API_IMAGE" . >/dev/null

echo "== caddy validate: all three TLS modes"
validate() { docker run --rm "$@" "$IMAGE" validate >/dev/null 2>&1; }
validate -e DOMAIN=example.org -e TLS_MODE=acme -e ACME_EMAIL=ops@example.org \
  || fail "acme config invalid"
validate -e DOMAIN=lab.internal -e TLS_MODE=internal || fail "internal config invalid"
validate -e DOMAIN=example.org -e TLS_MODE=acme || fail "acme without an e-mail must be valid"
validate -e DOMAIN=example.org -e TLS_MODE=acme -e ACME_EMAIL=you@example.org \
  || fail "acme with the example e-mail must be treated as unset"
mkdir -p "$work/certs"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=files.example.org" \
  -keyout "$work/certs/key.pem" -out "$work/certs/cert.pem" >/dev/null 2>&1
validate -v "$work/certs:/certs:ro" -e DOMAIN=files.example.org -e TLS_MODE=files \
  -e TLS_CERT_FILE=/certs/cert.pem -e TLS_KEY_FILE=/certs/key.pem || fail "files config invalid"

echo "== bad TLS configuration fails loudly"
expect_error() {
  local pattern=$1; shift
  if out=$(docker run --rm "$@" "$IMAGE" validate 2>&1); then fail "accepted bad config: $*"; fi
  echo "$out" | grep -q "$pattern" || fail "wrong error for $*: $out"
}
expect_error "TLS_MODE must be one of" -e DOMAIN=x.org -e TLS_MODE=bogus
expect_error "TLS_MODE must be one of" -e DOMAIN=x.org
expect_error "not a valid e-mail" -e DOMAIN=x.org -e TLS_MODE=acme -e "ACME_EMAIL=a b@x.org"
expect_error "not a valid e-mail" -e DOMAIN=x.org -e TLS_MODE=acme -e "ACME_EMAIL=x{y}@x.org"
expect_error "requires TLS_CERT_FILE" -e DOMAIN=x.org -e TLS_MODE=files
expect_error "not readable or empty" -e DOMAIN=x.org -e TLS_MODE=files -e TLS_CERT_FILE=/nope.pem \
  -e TLS_KEY_FILE=/nope.key
expect_error "DOMAIN must be set" -e TLS_MODE=internal

echo "== start api (real backend image, no database) + proxy (internal TLS)"
mkdir -p "$work/videos/1"
docker run --rm -u "$(id -u):$(id -g)" -v "$work/videos/1:/out" --entrypoint ffmpeg "$API_IMAGE" \
  -loglevel error -f lavfi -i testsrc=duration=3:size=320x240:rate=25 -pix_fmt yuv420p \
  -c:v libx264 /out/video.mp4
docker network create "$net" >/dev/null
docker run -d --name "${net}-api" --network "$net" --network-alias api \
  -e SECRET_KEY=proxy-test-secret-0123456789abcdef -e POSTGRES_PASSWORD=x -e POSTGRES_HOST=127.0.0.1 \
  -e API_WORKERS=1 "$API_IMAGE" >/dev/null
docker run -d --name "${net}-proxy" --network "$net" -p 127.0.0.1::443 -p 127.0.0.1::80 \
  -v "$work/videos:/data/videos:ro" \
  -e DOMAIN=localhost -e TLS_MODE=internal -e MAX_UPLOAD_CHUNK_MB=10 "$IMAGE" >/dev/null
https_port=$(docker port "${net}-proxy" 443/tcp | head -1 | awk -F: '{print $NF}')
http_port=$(docker port "${net}-proxy" 80/tcp | head -1 | awk -F: '{print $NF}')
base="https://localhost:${https_port}"

for _ in $(seq 1 60); do
  docker logs "${net}-api" 2>&1 | grep -q "Application startup complete" && break
  sleep 1
done
docker logs "${net}-api" 2>&1 | grep -q "Application startup complete" || fail "api did not start"
for _ in $(seq 1 60); do
  [ "$(curl -sk -o /dev/null -w '%{http_code}' "$base/" 2>/dev/null)" = "200" ] && break
  sleep 1
done

code() { curl -sk -o /dev/null -w '%{http_code}' "$@"; }
expect() {  # expect DESCRIPTION EXPECTED_CODE curl-args...
  local desc=$1 want=$2; shift 2
  local got; got=$(code "$@")
  [ "$got" = "$want" ] || fail "$desc: expected $want, got $got"
  echo "ok  $desc ($got)"
}

expect "frontend index" 200 "$base/"
expect "SPA fallback for deep route" 200 "$base/some/deep/route"
expect "admin login page proxied" 200 "$base/admin/login"
expect "api health proxied (503: no database)" 503 "$base/api/health"
expect "unknown api path reaches the api" 404 "$base/api/nope"
expect "/admin redirects to /admin/" 308 "$base/admin"
curl -sk -D - -o /dev/null "$base/admin" | grep -qi '^location: /admin/' || fail "/admin does not redirect to /admin/"
expect "video served" 200 "$base/media/videos/1.mp4"
expect "unknown video 404" 404 "$base/media/videos/999.mp4"
expect "non-numeric video id 404" 404 "$base/media/videos/abc.mp4"
expect "other /media paths 404" 404 "$base/media/other"
expect "range request" 206 -r 0-99 "$base/media/videos/1.mp4"
len=$(curl -sk -r 0-99 "$base/media/videos/1.mp4" | wc -c | tr -d ' ')
[ "$len" = "100" ] || fail "range body is $len bytes, expected 100"
curl -skI -r 0-99 "$base/media/videos/1.mp4" | grep -qi '^content-range: bytes 0-99/' \
  || fail "missing Content-Range"

echo "== video is a real MP4 and /data/videos is read-only"
curl -sk "$base/media/videos/1.mp4" -o "$work/dl.mp4"
docker run --rm -u "$(id -u):$(id -g)" -v "$work:/w:ro" --entrypoint ffprobe "$API_IMAGE" -v error \
  -select_streams v:0 -show_entries stream=codec_name,width,height -of csv=p=0 /w/dl.mp4 \
  | grep -q "h264,320,240" || fail "served file is not the expected MP4"
if docker exec "${net}-proxy" sh -c 'touch /data/videos/x' 2>/dev/null; then
  fail "/data/videos is writable"
fi

echo "== security headers"
headers=$(curl -skI "$base/")
for h in "strict-transport-security: max-age=" "permissions-policy: camera=(self)" \
         "x-content-type-options: nosniff" "content-security-policy:.*wasm-unsafe-eval"; do
  echo "$headers" | grep -qi "$h" || fail "missing header: $h"
done
echo "$headers" | grep -qi '^server:' && fail "Server header not removed"
echo "$headers" | grep -qi '^alt-svc:' && fail "HTTP/3 is advertised but UDP 443 is not published"
echo "ok  headers"

echo "== HTTP redirects to HTTPS"
redirect=$(curl -s -o /dev/null -w '%{http_code} %{redirect_url}' "http://localhost:${http_port}/")
echo "$redirect" | grep -qE '^30[78] https://localhost' || fail "no https redirect: $redirect"
echo "ok  $redirect"

echo "== request body limit (10 MiB on /api)"
# The real chunk endpoint does not exist yet and an upstream that ignores the body would hide the
# limit, so a second proxy talks to a stub `api` that reads the whole body, like the chunk route will.
cat > "$work/stub.py" <<'PY'
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_PUT(self):
        self.drain()

    def do_POST(self):
        self.drain()

    def drain(self):
        remaining = int(self.headers["Content-Length"])
        got = 0
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            got += len(chunk)
            remaining -= len(chunk)
        body = str(got).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Echo what the proxy forwarded, for the X-Forwarded-For check.
        body = self.headers.get("X-Forwarded-For", "").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
PY
net2="${net}-b"
docker network create "$net2" >/dev/null
docker run -d --name "${net}-stub" --network "$net2" --network-alias api \
  -v "$work/stub.py:/stub.py:ro" --entrypoint python "$API_IMAGE" /stub.py >/dev/null
docker run -d --name "${net}-proxy2" --network "$net2" -p 127.0.0.1::443 \
  -e DOMAIN=localhost -e TLS_MODE=internal -e MAX_UPLOAD_CHUNK_MB=10 -e MAX_VIDEO_UPLOAD_MB=2 \
  "$IMAGE" >/dev/null
base2="https://localhost:$(docker port "${net}-proxy2" 443/tcp | head -1 | awk -F: '{print $NF}')"
for _ in $(seq 1 60); do
  [ "$(code "$base2/")" = "200" ] && break
  sleep 1
done
head -c 1024 /dev/zero > "$work/small.bin"
head -c 10485760 /dev/zero > "$work/exact.bin"
head -c 10485761 /dev/zero > "$work/over.bin"
for f in small exact; do
  r=$(code -X PUT --data-binary @"$work/$f.bin" "$base2/api/sessions/x/chunks/webcam/0")
  [ "$r" = "200" ] || fail "$f body: expected 200, got $r"
done
r=$(code -X PUT --data-binary @"$work/over.bin" "$base2/api/sessions/x/chunks/webcam/0")
[ "$r" = "413" ] || fail "10 MiB + 1 byte: expected 413, got $r"
echo "ok  1 KiB and exactly 10 MiB accepted, 10 MiB + 1 byte rejected with 413"

echo "== the api sees the real client address, whatever the client puts in X-Forwarded-For"
# api/participant.py takes the RIGHTMOST entry as the client address (rate limiting). That is only
# safe if the proxy does not pass a client-supplied value through as the last entry.
seen=$(curl -sk -H "X-Forwarded-For: 1.2.3.4" "$base2/api/whoami")
last="${seen##*,}"; last="${last// /}"
[ -n "$last" ] || fail "the api received no X-Forwarded-For header from the proxy"
[ "$last" != "1.2.3.4" ] || fail "a client-supplied X-Forwarded-For reached the api as the last entry: '$seen'"
echo "ok  client sent 1.2.3.4, the api's last entry is the real peer ($last)"

echo "== admin video upload cap (MAX_VIDEO_UPLOAD_MB=2 in this test) only on /admin/api/videos"
head -c 2097152 /dev/zero > "$work/v2.bin"
head -c 2097153 /dev/zero > "$work/v2over.bin"
r=$(code -X POST --data-binary @"$work/v2.bin" "$base2/admin/api/videos")
[ "$r" = "200" ] || fail "video upload of exactly 2 MiB: expected 200, got $r"
r=$(code -X POST --data-binary @"$work/v2over.bin" "$base2/admin/api/videos")
[ "$r" = "413" ] || fail "video upload of 2 MiB + 1: expected 413, got $r"
r=$(code -X POST --data-binary @"$work/v2over.bin" "$base2/admin/api/videos/7")
[ "$r" = "413" ] || fail "video sub-path upload over cap: expected 413, got $r"
echo "ok  videos capped at 2 MiB"

echo "== other admin requests, including the login form, are capped (MAX_ADMIN_REQUEST_MB default 1)"
head -c 1048576 /dev/zero > "$work/a1.bin"
head -c 1048577 /dev/zero > "$work/a1over.bin"
for path in /admin/login /admin/logout /admin/api/other; do
  r=$(code -X POST --data-binary @"$work/a1.bin" "$base2$path")
  [ "$r" = "200" ] || fail "$path with exactly 1 MiB: expected 200, got $r"
  r=$(code -X POST --data-binary @"$work/a1over.bin" "$base2$path")
  [ "$r" = "413" ] || fail "$path with 1 MiB + 1 byte: expected 413, got $r"
done
r=$(code -X POST --data-binary @"$work/a1over.bin" "$base2/admin/api/videos")
[ "$r" = "200" ] || fail "the small admin cap must not apply to video uploads: got $r"
echo "ok  exactly 1 MiB accepted, 1 MiB + 1 byte rejected with 413; video path keeps its own cap"

echo "== bundled MediaPipe files are self-hosted and intact"
expect "wasm runtime" 200 "$base/mediapipe/wasm/vision_wasm_internal.wasm"
expect "wasm no-simd runtime" 200 "$base/mediapipe/wasm/vision_wasm_nosimd_internal.wasm"
curl -skI "$base/mediapipe/wasm/vision_wasm_internal.wasm" | grep -qi '^content-type: application/wasm' \
  || fail "wasm content-type is not application/wasm"
want=$(python3 -c "import json; print(json.load(open('shared/models.json'))['face_landmarker']['sha256'])")
got=$(curl -sk "$base/mediapipe/face_landmarker.task" | sha256 - | cut -d' ' -f1)
[ "$want" = "$got" ] || fail "served face_landmarker.task hash $got != $want"
echo "ok  face_landmarker.task sha256 matches shared/models.json"
expect "face model worker (classic script)" 200 "$base/mediapipe/face-worker.js"
curl -skI "$base/mediapipe/face-worker.js" | grep -qiE '^content-type: (text|application)/javascript' \
  || fail "face-worker.js is not served as JavaScript"
curl -sk "$base/mediapipe/face-worker.js" -o "$work/face-worker.js"
grep -q "importScripts" "$work/face-worker.js" \
  || fail "face-worker.js does not look like the bundled MediaPipe worker (no importScripts)"
echo "ok  face-worker.js is served as JavaScript and loads MediaPipe through importScripts"

echo "ALL PROXY CHECKS PASSED"
