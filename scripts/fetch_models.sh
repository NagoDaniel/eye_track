#!/usr/bin/env bash
# Download the Pipeline B model weights into MODELS_DIR and verify their sha256 (PLAN §11.2).
# URLs, sizes, hashes and file names come from shared/models.json (single source of truth).
# Needs only Docker: the download runs in a throw-away Python container.
#
# Usage: scripts/fetch_models.sh [--env-file FILE] [--manifest FILE] [NAME...]
#   NAME   a model from the manifest; default: l2cs_resnet50 (primary).
#          l2cs_mobileone_s0 is the optional small CPU fallback.
#   Files that already match their sha256 are kept; corrupted ones are replaced; a failed
#   download never overwrites an existing file. The worker also runs without these weights
#   (Pipeline B jobs then fail with a clear message).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT/.env"
MANIFEST="$ROOT/shared/models.json"
IMAGE="${FETCH_MODELS_IMAGE:-python:3.11-slim}"
DEFAULT_MODELS="l2cs_resnet50"

die() { echo "error: $*" >&2; exit 1; }

names=()
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) [ $# -ge 2 ] || die "--env-file needs a value"; ENV_FILE="$2"; shift ;;
    --manifest) [ $# -ge 2 ] || die "--manifest needs a value"; MANIFEST="$2"; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *) names+=("$1") ;;
  esac
  shift
done
[ ${#names[@]} -gt 0 ] || read -r -a names <<< "$DEFAULT_MODELS"

command -v docker >/dev/null 2>&1 || die "docker is not installed"
[ -f "$MANIFEST" ] || die "manifest not found: $MANIFEST"

models_dir=""
if [ -f "$ENV_FILE" ]; then
  models_dir="$(grep -E '^MODELS_DIR=' "$ENV_FILE" | tail -1 | cut -d= -f2- || true)"
fi
models_dir="${models_dir:-./models}"
case "$models_dir" in /*) ;; *) models_dir="$ROOT/${models_dir#./}" ;; esac
mkdir -p "$models_dir"
models_dir="$(cd "$models_dir" && pwd)"

manifest_dir="$(cd "$(dirname "$MANIFEST")" && pwd)"
echo "==> Fetching ${names[*]} into $models_dir" >&2
docker run --rm -u "$(id -u):$(id -g)" \
  -v "$ROOT/shared/download_model.py:/app/download_model.py:ro" \
  -v "$manifest_dir:/manifest:ro" \
  -v "$models_dir:/models" \
  "$IMAGE" python /app/download_model.py fetch --manifest "/manifest/$(basename "$MANIFEST")" /models "${names[@]}"
