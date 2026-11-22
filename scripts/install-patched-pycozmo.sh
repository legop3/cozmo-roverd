#!/usr/bin/env bash
# Build/install the tested pycozmo frame-sequence fix from a pinned upstream commit.
# No runtime monkeypatches; future upstream changes must be reviewed explicitly.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${1:-/opt/cozmo-roverd/.venv/bin/python}"
UPSTREAM="https://github.com/zayfod/pycozmo.git"
COMMIT="26b23c099c835fc86bcf9b7f454eb1110ec07488"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git clone --quiet --no-checkout "$UPSTREAM" "$WORK/pycozmo"
git -C "$WORK/pycozmo" checkout --quiet "$COMMIT"
git -C "$WORK/pycozmo" apply --check "$ROOT/patches/pycozmo-udp-frame-sequence.patch"
git -C "$WORK/pycozmo" apply "$ROOT/patches/pycozmo-udp-frame-sequence.patch"
"$PYTHON" -m pip install --no-deps --force-reinstall "$WORK/pycozmo"
"$PYTHON" - <<'PY'
import inspect
import pycozmo
from pycozmo.conn import SendThread
source = inspect.getsource(SendThread._send_packets)
assert "self._send_frame(to_frame, first_seq, last_seq, last_ack)" in source, (
    "The patched pycozmo module was not installed")
print("Installed validated pycozmo UDP frame-sequence patch:", pycozmo.__file__)
PY
