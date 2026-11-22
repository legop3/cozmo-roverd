#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dnf install -y python3 python3-pip python3-devel gcc git NetworkManager ffmpeg-free || {
  echo "Fedora ffmpeg-free package unavailable; install a Fedora-compatible ffmpeg providing libx264 and retry." >&2
  exit 1
}
install -d /opt/cozmo-roverd
# Install from the working checkout; do not overwrite user config on upgrades.
python3 -m venv /opt/cozmo-roverd/.venv
/opt/cozmo-roverd/.venv/bin/pip install --upgrade pip setuptools wheel
/opt/cozmo-roverd/.venv/bin/pip install "$ROOT"
if [[ ! -e /etc/cozmo-roverd.toml ]]; then
  install -m 0600 "$ROOT/config.example.toml" /etc/cozmo-roverd.toml
  echo "Created /etc/cozmo-roverd.toml; edit the MRR URL, adapter, Cozmo SSID and password before starting."
fi
install -m 0644 "$ROOT/packaging/cozmo-roverd.service" /etc/systemd/system/cozmo-roverd.service
systemctl daemon-reload
echo "Installed. Run: sudo /opt/cozmo-roverd/.venv/bin/cozmo-roverd --check"
echo "Then: sudo systemctl enable --now cozmo-roverd"
