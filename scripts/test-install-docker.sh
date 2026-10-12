#!/usr/bin/env bash
# Tests scripts/install-docker.sh in throw-away Debian and Ubuntu containers. Run via
# `make test-install-docker`. Downloads Docker's packages for real (a few hundred MB per image).
set -euo pipefail

cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }
IMAGES="${INSTALL_DOCKER_TEST_IMAGES:-ubuntu:24.04 debian:12}"

# in_container IMAGE SCRIPT: run a bash script as root in a fresh container with the installer at /install.sh
in_container() {
  local image=$1 script=$2
  docker run --rm -v "$PWD/scripts/install-docker.sh:/install.sh:ro" "$image" bash -c "$script"
}

echo "== shellcheck"
docker run --rm -v "$PWD:/mnt" koalaman/shellcheck:stable scripts/install-docker.sh scripts/test-install-docker.sh \
  || fail "shellcheck reported problems"

echo "== --help works; unknown options and a missing --add-user value are rejected"
./scripts/install-docker.sh --help | grep -q -- "--add-user" || fail "help text missing"
out=$(./scripts/install-docker.sh --bogus 2>&1) && fail "unknown option accepted"
echo "$out" | grep -q "unknown option" || fail "wrong error: $out"
out=$(./scripts/install-docker.sh --add-user 2>&1) && fail "--add-user without a value accepted"
echo "$out" | grep -q "needs a user name" || fail "wrong error: $out"

echo "== unsupported systems and unknown releases fail with a clear message (no installation attempted)"
fake=$(mktemp /tmp/eyetrack-os-release.XXXXXX)
trap 'rm -f "$fake"' EXIT
printf 'ID=fedora\nVERSION_CODENAME=\n' > "$fake"
out=$(docker run --rm -v "$PWD/scripts/install-docker.sh:/install.sh:ro" -v "$fake:/fake:ro" \
  -e INSTALL_DOCKER_OS_RELEASE=/fake ubuntu:24.04 /install.sh 2>&1) && fail "fedora accepted"
echo "$out" | grep -q "supports Ubuntu and Debian only (found 'fedora')" || fail "wrong error: $out"
printf 'ID=ubuntu\nVERSION_CODENAME=nonexistentcodename\n' > "$fake"
out=$(docker run --rm -v "$PWD/scripts/install-docker.sh:/install.sh:ro" -v "$fake:/fake:ro" \
  -e INSTALL_DOCKER_OS_RELEASE=/fake ubuntu:24.04 /install.sh 2>&1) && fail "unknown codename accepted"
echo "$out" | grep -q "Docker has no apt repository for ubuntu 'nonexistentcodename'" || fail "wrong error: $out"
echo "ok"

echo "== a non-root user without sudo is told to use root"
out=$(in_container ubuntu:24.04 'useradd -m plain && su plain -c "/install.sh"' 2>&1) && fail "non-root accepted"
echo "$out" | grep -q "must run as root" || fail "wrong error: $out"

for image in $IMAGES; do
  echo "== real installation on $image"
  in_container "$image" '
    set -e
    # First run as a non-root user with sudo: the options must survive the sudo re-run.
    apt-get update -qq && apt-get install -y -qq sudo >/dev/null
    useradd -m alice
    useradd -m operator1
    echo "operator1 ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/operator1
    su operator1 -c "/install.sh --add-user alice" >/tmp/first.log 2>&1 \
      || { cat /tmp/first.log; echo "first run failed"; exit 1; }
    grep -q "re-running with sudo" /tmp/first.log
    docker --version
    docker compose version
    dpkg -s docker-ce docker-compose-plugin | grep -c "^Status: install ok installed" | grep -qx 2
    test -f /etc/apt/keyrings/docker.asc
    grep -q "signed-by=/etc/apt/keyrings/docker.asc" /etc/apt/sources.list.d/docker.list
    id -nG alice | tr " " "\n" | grep -qx docker
    echo "--- second run is a no-op"
    /install.sh 2>&1 | tee /tmp/second.log
    grep -q "already installed" /tmp/second.log
    echo "--- unknown user is rejected on an installed system"
    ! /install.sh --add-user nobody-here >/dev/null 2>&1
  ' || fail "installation on $image failed"
  echo "ok  $image"
done

echo "ALL INSTALL-DOCKER CHECKS PASSED"
