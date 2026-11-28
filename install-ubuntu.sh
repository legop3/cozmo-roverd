#!/usr/bin/env bash
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "Run with sudo" >&2; exit 1; }

# =============================================================================
# Cozmo RoverD - Ubuntu Installation Script
# =============================================================================
# This script installs the Cozmo to MultiRoombaRover bridge on Ubuntu.
# It's fully compatible with Ubuntu 20.04, 22.04, and 24.04 LTS.
# =============================================================================

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_NAME=$(basename "$0")
log() { echo "[$SCRIPT_NAME - $(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }

echo "=========================================="
echo "$SCRIPT_NAME - Cozmo RoverD for Ubuntu"
echo "=========================================="
echo ""

# Step 1: Update package lists and install dependencies
log "Step 1: Updating packages and installing dependencies..."
sudo apt update -y

# Install Python, pip, NetworkManager and core packages (works on most Ubuntu systems)
sudo apt install -y software-properties-common gnupg2 curl \
    python3 python3-pip python3-venv git network-manager ffmpeg espeak-ng flite \
    curl unzip xz-utils imagemagick libavcodec-extra libavformat-extra

# Verify FFmpeg installation and encoders
log "Step 2: Verifying FFmpeg installation..."
if ! command -v ffmpeg >/dev/null 2>&1; then
    log "FFmpeg not found; attempting to install..."
    sudo apt install -y ffmpeg libavcodec-extra
fi

# Check for H.264 encoder support (Ubuntu's ffmpeg has it by default)
encoder_list="$(ffmpeg -hide_banner -encoders 2>/dev/null || true)"
if ! echo "$encoder_list" | grep -qE 'libx264|libopenh264|h264_vaapi'; then
    log "WARNING: No H.264 encoder found; video streaming may fail" >&2
fi

# Step 3: Set up Google Chrome TTS (optional audio feature)
log "Step 3: Setting up Google Chrome TTS assets..."
if ! bash "$ROOT/scripts/setup-tts.sh"; then
    log "Warning: TTS setup failed; hardware audio will use eSpeak/Flite" >&2
fi

# Step 4: Create installation directory and virtual environment
log "Step 4: Creating installation directory and Python venv..."
install -d /opt/cozmo-roverd
python3 -m venv /opt/cozmo-roverd/.venv

# Step 5: Install cozmo-roverd from current checkout
log "Step 5: Installing cozmo-roverd package..."
PYTHON="/opt/cozmo-roverd/.venv/bin/python"
"${PYTHON}" -m pip install --upgrade pip setuptools wheel

"${PYTHON}" -m pip install "$ROOT"

# Step 6: Install patched pycozmo dependency
log "Step 6: Installing patched pycozmo..."
bash "$ROOT/scripts/install-patched-pycozmo.sh" "$PYTHON"

# Step 7: Create and configure system configuration
log "Step 7: Setting up system configuration..."
install -d /etc/cozmo-roverd

if [[ ! -e /etc/cozmo-roverd.toml ]]; then
    install -m 0600 "$ROOT/config.example.toml" /etc/cozmo-roverd.toml
    log "Created /etc/cozmo-roverd.toml"
    echo ""
    log "IMPORTANT: Edit /etc/cozmo-roverd.toml with your Cozmo Wi-Fi credentials:"
    log "   - ssid = \"Cozmo_YourSSID\""
    log "   - password = \"password_shown_on_robot_display\""
fi

# Step 8: Copy systemd service files
log "Step 8: Installing systemd services..."
install -m 0644 "$ROOT/packaging/cozmo-roverd.service" /etc/systemd/system/cozmo-roverd.service
install -m 0644 "$ROOT/packaging/cozmo-roverd-setup.service" /etc/systemd/system/cozmo-roverd-setup.service

# Step 9: Enable and start services
log "Step 9: Enabling and starting services..."
systemctl daemon-reload
sudo systemctl enable --now cozmo-roverd-setup.service
sudo systemctl restart cozmo-roverd-setup.service

if sudo systemctl is-active --quiet cozmo-roverd.service; then
    log "Restarting cozmo-roverd.service to pick up new code..."
    sudo systemctl restart cozmo-roverd.service
fi

# Step 10: Firewall configuration (optional - uncomment if needed)
log "Step 10: Firewall configuration..."
log "Note: Local use doesn't require firewall rules."
log "If exposing port 8765 to untrusted networks:"
echo ""
echo "# Uncomment and run these lines to enable firewall protection:"
echo "sudo apt install -y ufw"
echo "sudo ufw allow in 8765/tcp"
echo "sudo systemctl restart ufw"

# Final completion messages
echo ""
echo "=========================================="
echo "$SCRIPT_NAME complete!"
echo "=========================================="
echo ""
echo "To start the setup service: sudo systemctl start cozmo-roverd-setup.service"
echo "To view the status: sudo systemctl status cozmo-roverd-setup.service"
echo ""
echo "Open http://<YOUR-UBUNTU-HOST-IP>:8765 from another computer"
echo "to configure the bridge. Enter your Cozmo Wi-Fi credentials shown"
echo "on the robot's screen."
echo ""
echo "Upgrade: sudo bash $SCRIPT_NAME"
echo ""