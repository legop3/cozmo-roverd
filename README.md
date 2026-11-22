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
4. **Rover:** choose the name, color, **Private rover** mode, max wheel speed, and camera exposure and video settings with simple controls. Click **Save & connect Cozmo**.

The setup UI validates data, keeps existing passwords when appropriate, writes `/etc/cozmo-roverd.toml` atomically with 0600 permissions and maintains a `.bak` copy, enables the rover service, and restarts it with the new settings. The final panel shows service state, recent logs and start/stop controls.

To upgrade an existing installation, `git pull` then rerun `sudo bash install-fedora.sh`. Existing configuration is preserved. You do **not** have to manually edit the TOML. The web UI uses the same configuration file as the daemon.

The setup page listens on `0.0.0.0:8765` and does not change MRR itself. **There is no authentication or encryption**: anyone who can reach this port can change robot networking and restart the service. Keep port 8765 on a trusted LAN and do not forward it onto the internet.

## Controls and integration

The **Private rover** toggle maps to MRR's native `private.enabled` property. It does not change access to the independent LAN setup dashboard.

- **Drive:** MRR's `driveDirect` sets continuous wheel speeds (no command-silence timeout). The hardware-supported maximum is 200 mm/s. New installations default to 200; existing configuration is preserved on upgrades, so select 200 in the dashboard if yours is still set to 100.
- **Head/camera:** MRR's camera servo tilts Cozmo's head. The existing headlight role controls its actual camera illuminator. Backpack LEDs are a separate toggle.
- **Lift:** Both the generic lift-height slider and the Roomba-shaped **main brush** control are supported. Signed main-brush PWM from -127 to 127 drives Cozmo's lift continuously down/up; zero stops the lift. Other brush/vacuum channels have no hardware equivalent and are ignored.
- **Audio:** MRR's TTS button sends speech to Cozmo's own speaker via offline eSpeak NG and PyCozmo audio; the generic volume slider changes the speaker's output level. Audio playback requires PyCozmo's animation controller, which is now enabled without enabling the procedural face.
- **Cliff:** Cozmo exposes one physical underside cliff detector, not four directional Roomba detectors. Its detected/not-detected flag is projected into MRR's *front-left* cliff channel as a compatibility mapping; the four raw samples are retained in the bridge's full telemetry. We deliberately do not invent other three detector readings. Cliff detection is telemetry-only: both the firmware cliff-stop and bridge-level cliff motor stop are disabled, so the sensor cannot interrupt or reject driving. Pickup stop behavior remains; orientation itself does **not** restrict driving.
- **Docking:** `undock` automatically reverses Cozmo off the charging contacts until contact clears, with a 3-second operation limit, and returns MRR's asynchronous `commandResult`. `dockAssist` stops and lowers the camera for user-guided docking, caps wheel speed at 60 mm/s, and stops/resets the camera when charger contact is detected. `cancelDocking` exits assisted docking. **Automatic charger-seeking** is not yet implemented: without charger localization it would be unreliable, so `autoDock` explicitly reports unsupported instead of pretending to dock.
- **Battery:** Cozmo exposes battery voltage and on-charger/charging flags, not a calibrated battery percentage. The bridge sends an approximate voltage-based percentage in MRR's authoritative `roverState`. This percentage can fluctuate under load; charging is based on actual status flags rather than assuming all docked time is charging.

On an MRR connection loss or daemon shutdown, motion is stopped. No stale driving command is replayed after reconnect.

## Video

Camera images are published via FFmpeg to the server's existing MediaMTX RTSP endpoint, default `rtsp://SERVER:8554/<rover.name>`. Resolution: 320x240, color, up to 15 FPS, H.264 low-latency settings. Camera presets use PyCozmo's native SetCameraParams exposure/gain control: **Auto**, **Bright**, or **Low light**, with gain/exposure clamped to the robot's reported camera limits. Software H.264 is preferred at this small resolution; VA-API is only attempted if neither software encoder works. The server must accept RTSP publishes. If the default MediaMTX RTSP port is not 8554, configure `media.rtsp_url` explicitly.

**Note:** Fedora's `ffmpeg-free` build may not contain `libx264`. The bridge automatically uses `libopenh264` when available instead. Enable the official `fedora-cisco-openh264` repository and install `openh264` if necessary, or install a full FFmpeg build. Check `ffmpeg -encoders | grep -E 'libx264|libopenh264'`. The installer does not silently enable third-party repositories.

## Live Cozmo telemetry & latency diagnostics

The setup website at `http://FEDORA-IP:8765` now includes a **Live Cozmo telemetry** panel under Status & diagnostics. It shows the actual on-charger and actively-charging flags separately, battery voltage and its approximate charge estimate, pickup/cliff/moving status, wheel speeds, head/lift positions, orientation and pose. Expand **All PyCozmo diagnostics** to see acceleration, gyroscope, all 17 robot status bits, hardware identity, camera details, animation counters, cube discovery/connection details and publisher health. These are also available from `GET /api/telemetry` on the setup web server. A one-second snapshot is written to `/run/cozmo-roverd/telemetry.json`, independently of MRR.

While Cozmo rests on its charger, its RobotState stream may pause or slow. The bridge now **retains the last measured battery voltage and estimated percentage**, as well as the last observed dock/charging flags, rather than clearing them after 3 seconds or restarting PyCozmo after 5 seconds of silence. The setup diagnostics expose `telemetry_stale` and `telemetry_age_seconds`: a retained value is not a fresh measurement, and no software can extrapolate live charging voltage without new packets. A genuine Wi-Fi link failure still triggers reconnection.

MRR's existing `/rover` WebSocket receives its full `roverState` at connect and again at least once per second (battery/charger/contact changes are sent sooner). Supported direct-json sensor readings are sent at 5 Hz: wheel speeds, voltage, charger contact, and the available cliff indicator with an averaged raw signal. The server's sensor field list is fixed, so Cozmo-only pose, accelerometer, gyro, lift and status flags **cannot simply be added as arbitrary sensor fields** without the server rejecting the entire message. Rather than editing MRR, the separate bridge exposes them in its own dashboard.

Camera frames are never buffered more than one frame in the bridge: stale frames are discarded, and FFmpeg uses input wall-clock timestamps, zero B-frames, fast low-delay encoding, and minimal RTSP mux delay. The telemetry panel reports frames received, dropped, encoder, frame age and publisher status to help distinguish Cozmo/bridge delay from downstream MRR RTSP/WebRTC playback buffering. **End-to-end latency depends on MRR's media playback pipeline too**, so bridge-side improvements alone do not guarantee a specific delay.

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
