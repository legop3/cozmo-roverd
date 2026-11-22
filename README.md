# Cozmo RoverD

Standalone Fedora Linux bridge that makes an Anki Cozmo appear as a rover on an existing [MultiRoombaRover](https://github.com/legop3/MultiRoombaRover) server. **It does not install or modify anything in the MRR repository or on the server.** It uses [PyCozmo](https://github.com/zayfod/pycozmo) and the current MRR rover WebSocket protocol.

## Hardware/network

Use **two network paths**: an Ethernet connection or primary Wi-Fi for reaching MRR, and a **second, dedicated Wi-Fi adapter** for Cozmo. Cozmo provides its own Wi-Fi access point. This bridge binds a NetworkManager connection exclusively to the adapter chosen in config, disables the default route on that connection and uses it only to communicate with Cozmo. Do **not** choose the host's normal uplink adapter. The SSID and password must be read from Cozmo's screen.

## Fedora installation and guided setup

Plug a **second Wi-Fi adapter** into your Fedora computer. Keep Ethernet or your primary Wi-Fi connected to the MRR network. The setup UI explicitly protects adapters used for default routes.

```bash
git clone https://github.com/legop3/cozmo-roverd.git
cd cozmo-roverd
sudo bash install-fedora.sh
```

**From another computer on the network, open `http://FEDORA-HOST-IP:8765`.** The web interface is designed for headless servers: it listens on all interfaces and runs separately from the bridge, even when Cozmo is offline. No SSH tunnel, login, or HTTPS setup required. The installer opens TCP 8765 through active firewalld installations.

The four steps are:
1. **Adapter:** choose your dedicated Wi-Fi device from a dropdown; adapters carrying the host's default route are disabled.
2. **Cozmo:** display Cozmo's Wi-Fi credentials on the robot, click **Scan networks**, pick its SSID from the results and enter its password.
3. **MRR server:** enter the WebSocket URL (for example, `ws://192.168.1.100:8080/rover`), then click **Test server connection**. This checks TCP reachability; a successful test does not prove rover authorization or full protocol operation.
4. **Rover:** choose the name, color, **Private rover** mode, speed, stop timeout, and video settings with simple controls. Click **Save & connect Cozmo**.

The setup UI validates data, keeps existing passwords when appropriate, writes `/etc/cozmo-roverd.toml` atomically with 0600 permissions and maintains a `.bak` copy, enables the rover service, and restarts it with the new settings. The final panel shows service state, recent logs and start/stop controls.

To upgrade an existing installation, `git pull` then rerun `sudo bash install-fedora.sh`. Existing configuration is preserved. You do **not** have to manually edit the TOML. The web UI uses the same configuration file as the daemon.

The setup page listens on `0.0.0.0:8765` and does not change MRR itself. **There is no authentication or encryption**: anyone who can reach this port can change robot networking and restart the service. Keep port 8765 on a trusted LAN and do not forward it onto the internet.

## Controls

The **Private rover** toggle maps directly to MRR’s native `private.enabled` hello property (off by default). It affects how MRR treats the rover; it does not secure the setup website, which remains intentionally accessible on your LAN.\n\nNormal MRR drive commands control Cozmo's wheels (clamped to the configured speed). The camera servo maps to Cozmo's head tilt. The existing headlight control and a generic peripheral button control backpack LEDs. Generic peripherals expose lift height (0-100%) and a two-line monochrome face text display. The bridge reports Cozmo wheel speeds, battery voltage, cliff detection and charging status. It **does not fabricate** a battery percentage from voltage; percentage remains unavailable. Cozmo does not support Roomba auto-docking/brush commands, so those return explicit errors.

The local drive watchdog stops the robot on command silence, MRR disconnect, cliff detection, pickup or bridge shutdown. No stale movement commands replay after reconnect.

## Video

Camera images are published via FFmpeg to the server's existing MediaMTX RTSP endpoint, default `rtsp://SERVER:8554/<rover.name>`. Resolution: 320x240, up to 15 FPS, H.264 low-latency settings. The server must accept RTSP publishes. If the default MediaMTX RTSP port is not 8554, configure `media.rtsp_url` explicitly.

**Note:** Fedora's `ffmpeg-free` build may not contain `libx264`. The bridge automatically uses `libopenh264` when available instead. Enable the official `fedora-cisco-openh264` repository and install `openh264` if necessary, or install a full FFmpeg build. Check `ffmpeg -encoders | grep -E 'libx264|libopenh264'`. The installer does not silently enable third-party repositories.

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
- `ffmpeg -encoders | grep -E 'libx264|libopenh264'`: check video encoder.
- If Cozmo drops its hotspot, restore it on the robot and the service retries connection.

Security: Cozmo Wi-Fi credentials are stored in root-owned `/etc/cozmo-roverd.toml`; don't commit real credentials. The service intentionally runs as root so NetworkManager can activate the dedicated interface; it must be installed only on a trusted Linux host.
