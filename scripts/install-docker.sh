#!/usr/bin/env bash
# Install Docker Engine and the Compose plugin from Docker's official apt repository
# (Ubuntu and Debian). Run it once on a new server, before scripts/setup.sh.
#
# Usage: scripts/install-docker.sh [--add-user NAME]
#   --add-user NAME   also let NAME use Docker without sudo (adds NAME to the "docker" group;
#                     NAME must log in again). Not needed when you work as root.
#   -h, --help        show this help
#
# Needs root (it re-runs itself with sudo if you are not). Safe to run again: if Docker and the
# Compose plugin are already installed it does nothing. The packages come from
# https://download.docker.com; the repository key is fetched over HTTPS as in Docker's own guide.
set -euo pipefail

# Overridable for tests.
OS_RELEASE="${INSTALL_DOCKER_OS_RELEASE:-/etc/os-release}"
DOCKER_REPO_URL="${INSTALL_DOCKER_REPO_URL:-https://download.docker.com/linux}"

die() { echo "error: $*" >&2; exit 1; }
info() { echo "==> $*" >&2; }

# Kept for the sudo re-run below: the option loop consumes "$@".
orig_args=("$@")
add_user=""
while [ $# -gt 0 ]; do
  case "$1" in
    --add-user) [ $# -ge 2 ] || die "--add-user needs a user name"; add_user="$2"; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
  shift
done

if [ "$(id -u)" != 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "this script must run as root (sudo is not available)"
  info "Not root: re-running with sudo"
  # The ${a[@]+...} form is needed because bash 3.2 treats an empty array as unset under `set -u`.
  exec sudo -E "$0" ${orig_args[@]+"${orig_args[@]}"}
fi

add_to_group() {
  [ -n "$add_user" ] || return 0
  id "$add_user" >/dev/null 2>&1 || die "user '$add_user' does not exist"
  usermod -aG docker "$add_user"
  info "Added $add_user to the docker group; $add_user must log out and in again"
}

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  info "Docker and the Compose plugin are already installed: $(docker --version)"
  add_to_group
  exit 0
fi

[ -r "$OS_RELEASE" ] || die "cannot read $OS_RELEASE; is this a Debian or Ubuntu system?"
# shellcheck source=/dev/null  # /etc/os-release is a system file of simple KEY=value lines
os_id="$(. "$OS_RELEASE"; echo "${ID:-}")"
# shellcheck source=/dev/null
codename="$(. "$OS_RELEASE"; echo "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}")"
case "$os_id" in
  ubuntu|debian) ;;
  *) die "this script supports Ubuntu and Debian only (found '${os_id:-unknown}'). See https://docs.docker.com/engine/install/ for other systems." ;;
esac
[ -n "$codename" ] || die "cannot determine the release codename from $OS_RELEASE"
command -v apt-get >/dev/null 2>&1 || die "apt-get not found"

export DEBIAN_FRONTEND=noninteractive
info "Installing prerequisites"
apt-get update -qq
apt-get install -y -qq ca-certificates curl >/dev/null

info "Checking that Docker publishes packages for $os_id $codename"
status="$(curl -s -o /dev/null -w '%{http_code}' -m 30 "$DOCKER_REPO_URL/$os_id/dists/$codename/Release" || true)"
[ "$status" = 200 ] || die "Docker has no apt repository for $os_id '$codename' (HTTP $status). A very new release may not be supported yet; see https://docs.docker.com/engine/install/"

info "Adding the Docker repository"
install -m 0755 -d /etc/apt/keyrings
curl -fsSL -m 60 "$DOCKER_REPO_URL/$os_id/gpg" -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] $DOCKER_REPO_URL/$os_id $codename stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update -qq

info "Installing Docker Engine and the Compose plugin (this takes a minute or two)"
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null

# In containers and other systems without systemd there is no service to start.
if [ -d /run/systemd/system ]; then
  systemctl enable --now docker >/dev/null 2>&1 || die "Docker is installed but the service did not start; see: systemctl status docker"
fi
add_to_group

info "Installed: $(docker --version), $(docker compose version)"
