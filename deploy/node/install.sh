#!/usr/bin/env bash
# Install or refresh the MediaMTX camera node on this host. Idempotent -- safe to re-run.
# The repo's mediamtx.yml is the source of truth and overwrites the host copy each run.
# Run as your normal login user (it uses sudo for the privileged steps):
#   ./deploy/node/install.sh
set -euo pipefail

[ "${EUID:-$(id -u)}" -eq 0 ] && { echo "run as your normal user (the script uses sudo itself), not with sudo" >&2; exit 1; }

MTX_VERSION="v1.19.1"
RUN_USER="$USER"                           # MediaMTX runs as this user; must be in the 'video' group
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

case "$(uname -m)" in
  aarch64) ARCH="arm64" ;;
  armv7l)  ARCH="armv7" ;;
  armv6l)  ARCH="armv6" ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

tarball="mediamtx_${MTX_VERSION}_linux_${ARCH}.tar.gz"
url="https://github.com/bluenviron/mediamtx/releases/download/${MTX_VERSION}/${tarball}"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT

echo ">> downloading ${tarball}"
curl -fsSL -o "$tmp/mtx.tar.gz" "$url"
tar xzf "$tmp/mtx.tar.gz" -C "$tmp"
sudo install -m 0755 "$tmp/mediamtx" /usr/local/bin/mediamtx

echo ">> installing config + unit"
sudo mkdir -p /etc/mediamtx
sudo install -m 0644 "$SCRIPT_DIR/mediamtx.yml" /etc/mediamtx/mediamtx.yml
sed "s/__USER__/${RUN_USER}/g" "$SCRIPT_DIR/mediamtx.service" \
  | sudo tee /etc/systemd/system/mediamtx.service >/dev/null
sudo usermod -aG video "$RUN_USER" || true

sudo systemctl daemon-reload
sudo systemctl enable mediamtx
sudo systemctl restart mediamtx
echo ">> mediamtx ${MTX_VERSION} running -- stream at rtsp://$(hostname):8554/cam"