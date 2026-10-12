#!/usr/bin/env bash
# Tests scripts/fetch_models.sh (PLAN §11.2). Run via `make test-fetch-models`.
# Downloads the real 5 MB MobileOne weights once; everything else uses a local manifest.
set -euo pipefail

cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }
work=$(mktemp -d /tmp/eyetrack-test.XXXXXX)  # /tmp is shared with Docker Desktop on macOS; /var/folders is not
trap 'rm -rf "$work"' EXIT
envf="$work/env"
printf 'MODELS_DIR=%s/models\n' "$work" > "$envf"
fetch() { ./scripts/fetch_models.sh --env-file "$envf" "$@"; }
want=$(python3 -c "import json; print(json.load(open('shared/models.json'))['l2cs_mobileone_s0']['sha256'])")
file="$work/models/mobileone_s0_gaze.onnx"

echo "== shellcheck"
docker run --rm -v "$PWD:/mnt" koalaman/shellcheck:stable scripts/fetch_models.sh scripts/test-fetch-models.sh \
  || fail "shellcheck reported problems"

echo "== real download from GitHub is verified"
fetch l2cs_mobileone_s0 >/dev/null 2>"$work/err1" || { cat "$work/err1" >&2; fail "download failed"; }
[ "$(shasum -a 256 "$file" | cut -d' ' -f1)" = "$want" ] || fail "downloaded file has the wrong sha256"
[ "$(stat -f %u "$file" 2>/dev/null || stat -c %u "$file")" = "$(id -u)" ] || fail "file not owned by the calling user"
[ -z "$(find "$work/models" -name '*.tmp')" ] || fail "temporary file left behind"

echo "== second run keeps the file; a corrupted file is replaced"
out=$(fetch l2cs_mobileone_s0 2>&1) || fail "second run failed"
echo "$out" | grep -q "already present" || fail "did not report the file as present: $out"
head -c 1000 "$file" > "$work/trunc" && cp "$work/trunc" "$file"
out=$(fetch l2cs_mobileone_s0 2>&1) || fail "repair run failed"
echo "$out" | grep -q "downloaded" || fail "corrupted file was not replaced: $out"
[ "$(shasum -a 256 "$file" | cut -d' ' -f1)" = "$want" ] || fail "repaired file has the wrong sha256"

echo "== unknown names and unknown options fail loudly"
out=$(fetch nope 2>&1) && fail "unknown model accepted"
echo "$out" | grep -q "unknown model 'nope'" || fail "wrong error: $out"
fetch --bogus >/dev/null 2>&1 && fail "unknown option accepted"

echo "== wrong hash in the manifest fails and keeps the existing file"
printf 'payload' > "$work/src.bin"
python3 - "$work" <<'PY'
import json, sys
work = sys.argv[1]
json.dump({"m": {"url": "file:///manifest/src.bin", "sha256": "0" * 64, "bytes": 7, "file": "m.onnx"}},
          open(f"{work}/bad.json", "w"))
PY
printf 'existing' > "$work/models/m.onnx"
out=$(fetch --manifest "$work/bad.json" m 2>&1) && fail "wrong hash accepted"
echo "$out" | grep -q "sha256 mismatch" || fail "wrong error: $out"
[ "$(cat "$work/models/m.onnx")" = "existing" ] || fail "existing file was overwritten"
[ -z "$(find "$work/models" -name '*.tmp')" ] || fail "temporary file left behind"

echo "== local manifest with the right hash works"
python3 - "$work" <<'PY'
import hashlib, json, sys
work = sys.argv[1]
json.dump({"m": {"url": "file:///manifest/src.bin", "sha256": hashlib.sha256(b"payload").hexdigest(),
                 "bytes": 7, "file": "m.onnx"}}, open(f"{work}/good.json", "w"))
PY
fetch --manifest "$work/good.json" m >/dev/null 2>&1 || fail "good local manifest rejected"
[ "$(cat "$work/models/m.onnx")" = "payload" ] || fail "local model not written"

echo "ALL FETCH_MODELS CHECKS PASSED"
