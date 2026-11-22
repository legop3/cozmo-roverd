# Cozmo RoverD

Standalone Fedora Linux bridge that makes an Anki Cozmo appear as a rover on an existing [MultiRoombaRover](https://github.com/legop3/MultiRoombaRover) server. **It does not install or modify anything in the MRR repository or on the server.** It uses [PyCozmo](https://github.com/zayfod/pycozmo) and the current MRR rover WebSocket protocol.

## Hardware/network

Use **two network paths**: an Ethernet connection or primary Wi-Fi for reaching MRR, and a **second, dedicated Wi-Fi adapter** for Cozmo. Cozmo provides its own Wi-Fi access point. This bridge binds a NetworkManager connection exclusively to the adapter chosen in config, disables the default route on that connection and uses it only to communicate with Cozmo. Do **not** choose the host's normal uplink adapter. The SSID and password must be read from Cozmo's screen.

## Fedora installation

```bash
git clone https://github.com/legop3/cozmo-roverd.git
cd cozmo-roverd
sudo bash install-fedora.sh
sudo nano /etc/cozmo-roverd.toml
```

In the TOML, set `rover.server_url` to the existing MRR server's `ws://HOST:PORT/rover` (or `wss://`), `wifi.interface` to the dedicated adapter shown by `nmcli device status`, and `wifi.ssid` and `wifi.password` to the credentials Cozmo shows. The host must already be able to reach the MRR server through its **other** connection. Then:

```bash
sudo /opt/cozmo-roverd/.venv/bin/cozmo-roverd --check
sudo systemctl enable --now cozmo-roverd
journalctl -u cozmo-roverd -f
```

Use `sudo systemctl restart cozmo-roverd` after editing configuration. The systemd service starts automatically after reboot. This is a non-containerized Fedora install because it must manage a physical Wi-Fi adapter.

## Controls

Normal MRR drive commands control Cozmo's wheels (clamped to the configured speed). The camera servo maps to Cozmo's head tilt. The existing headlight control and a generic peripheral button control backpack LEDs. Generic peripherals expose lift height (0-100%) and a two-line monochrome face text display. The bridge reports Cozmo wheel speeds, battery voltage, cliff detection and charging status. It **does not fabricate** a battery percentage from voltage; percentage remains unavailable. Cozmo does not support Roomba auto-docking/brush commands, so those return explicit errors.

The local drive watchdog stops the robot on command silence, MRR disconnect, cliff detection, pickup or bridge shutdown. No stale movement commands replay after reconnect.

## Video

Camera images are published via FFmpeg to the server's existing MediaMTX RTSP endpoint, default `rtsp://SERVER:8554/<rover.name>`. Resolution: 320x240, up to 15 FPS, H.264 low-latency settings. The server must accept RTSP publishes. If the default MediaMTX RTSP port is not 8554, configure `media.rtsp_url` explicitly.

**Note:** Fedora's `ffmpeg-free` build may not contain the `libx264` encoder. Check `ffmpeg -encoders | grep libx264` and install a compatible FFmpeg build if absent. The installer does not silently enable third-party repositories.

## Status and current limitations

The bridge is designed against the documented live MRR protocol and the PyCozmo upstream source; **real robot, Wi-Fi and end-to-end server integration still require testing on actual hardware**. Management/config editing from MRR is intentionally not implemented (config belongs to this independent product). Unsupported robot features are never mapped to fake Roomba capabilities. PyCozmo accesses Cozmo without the official mobile app.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
```

## Troubleshooting

- `nmcli device status`: identify the dedicated Wi-Fi device and ensure it is not the uplink.
- `nmcli -f GENERAL,IP4 device show wlan1`: inspect Cozmo-only link.
- `journalctl -u cozmo-roverd -n 100 --no-pager`: view connection, command and media errors.
- `ffmpeg -encoders | grep libx264`: check video encoder.
- If Cozmo drops its hotspot, restore it on the robot and the service retries connection.

Security: Cozmo Wi-Fi credentials are stored in root-owned `/etc/cozmo-roverd.toml`; don't commit real credentials. The service intentionally runs as root so NetworkManager can activate the dedicated interface; it must be installed only on a trusted Linux host.
