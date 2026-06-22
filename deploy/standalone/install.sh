#!/usr/bin/env bash
# Install or refresh the self-contained birdcam (capture + recorder + frontend) on this host.
# Idempotent. Run as your normal login user from anywhere in the repo:
#   ./deploy/standalone/install.sh
# Python and frontend deps build as YOU; only the system files (/etc, units) use sudo --
# so do NOT run this with sudo, or .venv ends up root-owned.
set -euo pipefail

[ "${EUID:-$(id -u)}" -eq 0 ] && { echo "run as your normal user (the script uses sudo itself), not with sudo" >&2; exit 1; }

RUN_USER="$USER"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"     # repo root: deploy/standalone -> ../../

echo ">> repo=$REPO_DIR  user=$RUN_USER"

command -v uv  >/dev/null || { echo "uv not found -- see https://docs.astral.sh/uv/" >&2; exit 1; }
command -v npm >/dev/null || { echo "npm not found -- install Node.js" >&2; exit 1; }

echo ">> python deps (uv sync --extra standalone)"
( cd "$REPO_DIR" && uv sync --extra standalone )

echo ">> frontend deps (npm)"
( cd "$REPO_DIR/frontend" && if [ -f package-lock.json ]; then npm ci; else npm install; fi )

echo ">> host config in /etc/birdcam (seeded once, never clobbered)"
sudo mkdir -p /etc/birdcam
if [ ! -f /etc/birdcam/config.toml ]; then
  sudo install -m 0644 "$SCRIPT_DIR/config.example.toml" /etc/birdcam/config.toml
  echo "   wrote /etc/birdcam/config.toml -- set mic_device / high_res for THIS host"
else
  echo "   /etc/birdcam/config.toml exists, left as-is"
fi
if [ ! -f /etc/birdcam/env ]; then
  sudo install -m 0600 "$SCRIPT_DIR/env.example" /etc/birdcam/env
  sudo chown "$RUN_USER" /etc/birdcam/env
  echo "   wrote /etc/birdcam/env -- EDIT IT: set BIRDCAM_TOKEN to a long random string"
else
  echo "   /etc/birdcam/env exists, left as-is"
fi

echo ">> installing units (repo path + user filled in)"
for unit in birdcam.service birdcam-frontend.service; do
  sed -e "s#/path/to/birdcam#${REPO_DIR}#g" -e "s/yourUser/${RUN_USER}/g" \
      "$SCRIPT_DIR/$unit" | sudo tee "/etc/systemd/system/$unit" >/dev/null
done

sudo systemctl daemon-reload
sudo systemctl enable  birdcam.service birdcam-frontend.service
sudo systemctl restart birdcam.service birdcam-frontend.service
echo ">> done -- check: systemctl status birdcam | journalctl -fu birdcam"