#!/usr/bin/env bash
# Ubuntu (and Debian-family) installer for cozmo-roverd.
# Mirrors install-fedora.sh, using apt package names and ufw instead of firewalld.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export DEBIAN_FRONTEND=noninteractive

if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  if [[ "${ID:-}" != ubuntu && " ${ID_LIKE:-} " != *" ubuntu "* && " ${ID_LIKE:-} " != *" debian "* && "${ID:-}" != debian ]]; then
    echo "Warning: this installer targets Ubuntu; detected ${PRETTY_NAME:-unknown}. Use install-fedora.sh on Fedora." >&2
  fi
fi

apt-get update
# Fedora -> Ubuntu package names:
#   python3-devel -> python3-dev, (venv is split out) -> python3-venv
#   gcc -> build-essential, NetworkManager -> network-manager
#   libva-utils -> vainfo, xz -> xz-utils, dejavu-sans-fonts -> fonts-dejavu-core
# iproute2/iputils-ping/iw/rfkill provide ip, ping, iw and rfkill used by the
# setup UI and hardware tests (preinstalled on Fedora, not always on Ubuntu Server).
apt-get install -y --no-install-recommends \
  python3 python3-pip python3-dev python3-venv build-essential git ca-certificates \
  network-manager wpasupplicant iproute2 iputils-ping iw rfkill \
  vainfo espeak-ng flite curl unzip xz-utils fonts-dejavu-core

# Intel VA-API drivers (Fedora: libva-intel-driver) only exist for x86.
case "$(uname -m)" in
  x86_64|amd64)
    apt-get install -y --no-install-recommends i965-va-driver intel-media-va-driver \
      || echo "Warning: Intel VA-API drivers unavailable; software H.264 encoding is still used." >&2
    ;;
esac

# pyproject.toml requires Python >= 3.11. Ubuntu 22.04 ships 3.10 but offers 3.11.
PYTHON=python3
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  echo "System python3 is older than 3.11; installing python3.11..."
  if ! apt-get install -y --no-install-recommends python3.11 python3.11-venv python3.11-dev; then
    echo "Error: Python 3.11+ is required. Upgrade to Ubuntu 24.04 or newer." >&2
    exit 1
  fi
  PYTHON=python3.11
fi

# Ubuntu's ffmpeg package already includes libx264. Keep any existing install.
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "FFmpeg not found; installing Ubuntu's ffmpeg..."
  apt-get install -y --no-install-recommends ffmpeg
fi
# Capture the full encoder list first: grep -q in a pipe under pipefail
# can SIGPIPE ffmpeg and incorrectly claim its encoders are missing.
encoder_list="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
if ! grep -Eq 'libx264|libopenh264|h264_vaapi' <<< "$encoder_list"; then
  echo "Warning: FFmpeg lists no H.264 encoder. Video diagnostics in the service will explain further." >&2
fi

# The bridge and setup UI drive Wi-Fi through NetworkManager.
systemctl enable --now NetworkManager.service
if nmcli -t -f TYPE,STATE device 2>/dev/null | grep -q '^wifi:unmanaged$'; then
  echo "Warning: a Wi-Fi adapter is 'unmanaged' by NetworkManager (often netplan with systemd-networkd)." >&2
  echo "         Remove it from /etc/netplan so NetworkManager can control the dedicated Cozmo adapter." >&2
fi

if ! bash "$ROOT/scripts/setup-tts.sh"; then
  echo "Warning: Chrome Google TTS setup failed; eSpeak and Flite remain available." >&2
fi

install -d /opt/cozmo-roverd
# Install from the working checkout; do not overwrite user config on upgrades.
# Recreate the venv if it was built with a different Python (e.g. an older 3.10 attempt).
if [[ -x /opt/cozmo-roverd/.venv/bin/python ]] && \
   ! /opt/cozmo-roverd/.venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  rm -rf /opt/cozmo-roverd/.venv
fi
"$PYTHON" -m venv /opt/cozmo-roverd/.venv
/opt/cozmo-roverd/.venv/bin/pip install --upgrade pip setuptools wheel
/opt/cozmo-roverd/.venv/bin/pip install "$ROOT"
# Replace upstream dependency with the pinned, source-patched build.
bash "$ROOT/scripts/install-patched-pycozmo.sh" /opt/cozmo-roverd/.venv/bin/python

if [[ ! -e /etc/cozmo-roverd.toml ]]; then
  install -m 0600 "$ROOT/config.example.toml" /etc/cozmo-roverd.toml
  echo "Created /etc/cozmo-roverd.toml; complete setup in the web UI (address below)."
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

# Ubuntu uses ufw rather than firewalld; only touch it when it is enabled.
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  ufw allow 8765/tcp
elif systemctl is-active --quiet firewalld.service && command -v firewall-cmd >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=8765/tcp
  firewall-cmd --reload
fi

HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "Installed! From another computer open http://${HOST_IP:-UBUNTU-HOST-IP}:8765"
echo "The web page scans Wi-Fi, saves settings, and starts the bridge."
