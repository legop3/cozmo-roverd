#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Do not request ffmpeg-free alongside RPM Fusion's ffmpeg: the packages conflict.
# Preserve the host's existing FFmpeg installation and its encoders.
dnf install -y python3 python3-pip python3-devel gcc git NetworkManager
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "FFmpeg not found; installing a Fedora-compatible build..."
  if ! dnf install -y ffmpeg; then
    dnf install -y ffmpeg-free
  fi
fi
if ! ffmpeg -hide_banner -encoders 2>/dev/null | grep -Eq "libx264|libopenh264"; then
  echo "No H.264 encoder available. Install Fedora openh264 (fedora-cisco-openh264) or a full FFmpeg build before enabling video." >&2
fi
install -d /opt/cozmo-roverd
# Install from the working checkout; do not overwrite user config on upgrades.
python3 -m venv /opt/cozmo-roverd/.venv
/opt/cozmo-roverd/.venv/bin/pip install --upgrade pip setuptools wheel
/opt/cozmo-roverd/.venv/bin/pip install "$ROOT"
if [[ ! -e /etc/cozmo-roverd.toml ]]; then
  install -m 0600 "$ROOT/config.example.toml" /etc/cozmo-roverd.toml
  echo "Created /etc/cozmo-roverd.toml; complete setup using http://FEDORA-HOST-IP:8765"
fi
install -m 0644 "$ROOT/packaging/cozmo-roverd.service" /etc/systemd/system/cozmo-roverd.service
install -m 0644 "$ROOT/packaging/cozmo-roverd-setup.service" /etc/systemd/system/cozmo-roverd-setup.service
systemctl daemon-reload
systemctl enable --now cozmo-roverd-setup.service
systemctl restart cozmo-roverd-setup.service
if systemctl is-active --quiet firewalld.service && command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=8765/tcp
  firewall-cmd --reload
fi
echo "Installed! From another computer open http://FEDORA-HOST-IP:8765"
echo "The web page scans Wi-Fi, saves settings, and starts the bridge."
