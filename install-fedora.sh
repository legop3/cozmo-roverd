#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Do not request ffmpeg-free alongside RPM Fusion's ffmpeg: the packages conflict.
# Preserve the host's existing FFmpeg installation and its encoders.
dnf install -y python3 python3-pip python3-devel gcc git NetworkManager libva-intel-driver libva-utils espeak-ng dejavu-sans-fonts
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "FFmpeg not found; installing a Fedora-compatible build..."
  if ! dnf install -y ffmpeg; then
    dnf install -y ffmpeg-free
  fi
fi
# Capture the full encoder list first: grep -q in a pipe under pipefail
# can SIGPIPE ffmpeg and incorrectly claim its encoders are missing.
encoder_list="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
if ! grep -Eq 'libx264|libopenh264|h264_vaapi' <<< "$encoder_list"; then
  echo "Warning: FFmpeg lists no H.264 encoder. Video diagnostics in the service will explain further." >&2
fi
install -d /opt/cozmo-roverd
# Install from the working checkout; do not overwrite user config on upgrades.
python3 -m venv /opt/cozmo-roverd/.venv
/opt/cozmo-roverd/.venv/bin/pip install --upgrade pip setuptools wheel
/opt/cozmo-roverd/.venv/bin/pip install "$ROOT"
# Replace upstream dependency with the pinned, source-patched build.
bash "$ROOT/scripts/install-patched-pycozmo.sh" /opt/cozmo-roverd/.venv/bin/python
if [[ ! -e /etc/cozmo-roverd.toml ]]; then
  install -m 0600 "$ROOT/config.example.toml" /etc/cozmo-roverd.toml
  echo "Created /etc/cozmo-roverd.toml; complete setup using http://FEDORA-HOST-IP:8765"
fi
install -m 0644 "$ROOT/packaging/cozmo-roverd.service" /etc/systemd/system/cozmo-roverd.service
install -m 0644 "$ROOT/packaging/cozmo-roverd-setup.service" /etc/systemd/system/cozmo-roverd-setup.service
systemctl daemon-reload
systemctl enable --now cozmo-roverd-setup.service
systemctl restart cozmo-roverd-setup.service
# Upgrading the package does not reload an already running Python process.
# Restart an active bridge to pick up new code; leave unconfigured bridges off.
if systemctl is-active --quiet cozmo-roverd.service; then
  systemctl restart cozmo-roverd.service
fi
if systemctl is-active --quiet firewalld.service && command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=8765/tcp
  firewall-cmd --reload
fi
echo "Installed! From another computer open http://FEDORA-HOST-IP:8765"
echo "The web page scans Wi-Fi, saves settings, and starts the bridge."
