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
4. **Rover:** choose the name, optional description, color, **Private rover** mode, max wheel speed, and camera exposure and video settings with simple controls. Click **Save & connect Cozmo**.

The setup UI validates data, keeps existing passwords when appropriate, writes `/etc/cozmo-roverd.toml` atomically with 0600 permissions and maintains a `.bak` copy, enables the rover service, and restarts it with the new settings. The final panel shows service state, recent logs and start/stop controls.

To upgrade an existing installation, `git pull` then rerun `sudo bash install-fedora.sh`. Existing configuration is preserved. You do **not** have to manually edit the TOML. The web UI uses the same configuration file as the daemon.

The setup page listens on `0.0.0.0:8765` and does not change MRR itself. **There is no authentication or encryption**: anyone who can reach this port can change robot networking and restart the service. Keep port 8765 on a trusted LAN and do not forward it onto the internet.

## Controls and integration

The **Private rover** toggle maps to MRR's native `private.enabled` property. It does not change access to the independent LAN setup dashboard.

- **Drive:** MRR's `driveDirect` sets continuous wheel speeds (no command-silence timeout). PyCozmo's published maximum is 200 mm/s, but Anki's original remote-control SDK example sends 250 mm/s commands. The bridge now offers 250 mm/s (default on new installs); existing configuration is preserved on upgrades, so select 250 in the dashboard if yours is still set lower. Telemetry records configured cap, commanded wheel speeds, and measured wheel speeds to establish what the robot actually achieves.
- **Head/camera:** MRR's camera servo tilts Cozmo's head. The existing headlight role controls its actual camera illuminator. Backpack LEDs are reserved for automatic connection, docking and charging status; only the headlight remains manually controlled. **Status LEDs:** automated backpack and physical-dock LED control are **disabled by default** after repeatable Cozmo reliable-command ACK stalls, even with a single red-only `LightStateSide` packet. Both paths remain available only for deliberate hardware diagnostics via `COZMO_BACKPACK_STATUS=1` and `COZMO_CHARGER_STATUS=1` in the service environment (restart required). Do not enable these on a robot that needs reliable camera/driving until the protocol interaction is understood. The manual front headlight remains independently controllable.
- **Face status:** Cozmo's native 128×32 display shows battery voltage and the latest TTS message in readable wrapped text. The screen uses direct, bounded PyCozmo protocol packets; a low-rate animation-clock tick keeps the image visible. No custom PyCozmo animation methods are installed.
- **Lift:** Both the generic lift-height slider and the Roomba-shaped **main brush** control are supported. Signed main-brush PWM from -127 to 127 drives Cozmo's lift continuously down/up; zero stops the lift. Other brush/vacuum channels have no hardware equivalent and are ignored.
- **Audio:** Hardware TTS is disabled because playback never worked reliably and caused ACK stalls. MRR TTS text can still appear on Cozmo's display; speech requests return a clear unsupported-audio error.
- **Cliff:** Cozmo exposes one physical underside cliff detector, not four directional Roomba detectors. Its detected/not-detected flag is projected into MRR's *front-left* cliff channel as a compatibility mapping; the four raw samples are retained in the bridge's full telemetry. We deliberately do not invent other three detector readings. Cliff detection is telemetry-only: both the firmware cliff-stop and bridge-level cliff motor stop are disabled, so the sensor cannot interrupt or reject driving. Pickup, orientation, charger-contact and cliff flags are all telemetry-only: none can reject, throttle or stop manual drive commands. The bridge advertises disabled MRR private-safety overrides for this rover.
- **Docking:** `undock` automatically drives Cozmo **forward** off the charging contacts until contact clears, with a 3-second operation limit, and returns MRR's asynchronous `commandResult`. `dockAssist` prepares a user-guided approach; manual driving remains under direct MRR command even if dock contact occurs. `cancelDocking` exits assisted docking. **Automatic charger-seeking** is not yet implemented: without charger localization it would be unreliable, so `autoDock` explicitly reports unsupported instead of pretending to dock.
- **Battery:** Cozmo exposes battery voltage and on-charger/charging flags, not a calibrated battery percentage. The bridge sends an approximate voltage-based percentage in MRR's authoritative `roverState`. This percentage can fluctuate under load; charging is based on actual status flags rather than assuming all docked time is charging.

On an MRR connection loss or daemon shutdown, motion is stopped. No stale driving command is replayed after reconnect.

## Video

Camera images are published via FFmpeg to the server's existing MediaMTX RTSP endpoint, default `rtsp://SERVER:8554/<rover.name>`. Resolution: 320x240, color, up to 15 FPS, H.264 low-latency settings. Camera presets use PyCozmo's native SetCameraParams exposure/gain control: **Auto**, **Bright**, or **Low light**, with gain/exposure clamped to the robot's reported camera limits. Software H.264 is preferred at this small resolution; VA-API is only attempted if neither software encoder works. The server must accept RTSP publishes. If the default MediaMTX RTSP port is not 8554, configure `media.rtsp_url` explicitly.

**Note:** Fedora's `ffmpeg-free` build may not contain `libx264`. The bridge automatically uses `libopenh264` when available instead. Enable the official `fedora-cisco-openh264` repository and install `openh264` if necessary, or install a full FFmpeg build. Check `ffmpeg -encoders | grep -E 'libx264|libopenh264'`. The installer does not silently enable third-party repositories.

## PyCozmo packet framing fix

The bridge uses **upstream PyCozmo unchanged at runtime**: no send-thread or animation-controller method replacements. The unused experimental UDP frame-batching patch and failed animation/audio monkey-patch have been removed. Since upstream's animation controller would continuously enqueue display and silence frames, this bridge disables animation playback and sends its static face image directly as a standard `DisplayImage` packet with a limited `OutputSilence` clock (up to 4 Hz) and an image refresh when needed. The face path checks the reliable send window before enqueueing traffic. This preserves the existing HUD, camera, driving, and other control paths without invasive patches.

## Persistent reconnect diagnostics

The bridge writes **persistent, size-bounded diagnostics** to `/var/log/cozmo-roverd/`, independently of the systemd journal. These survive a service restart or a robot disconnect:

- `incidents.jsonl`: **append-only, never automatically rotated or deleted**, containing structured JSON events for Wi-Fi/robot/MRR connection stages and full disconnect snapshots **before** PyCozmo is cleaned up. Each snapshot contains the last 120 bridge events (including recent commanded wheel speeds and lift/head actions), 90 seconds of sampled reliable-ACK/send-window/RobotState/camera metrics, and metadata for the last 300 PyCozmo packet send/receive events.
- `protocol.log` (plus `.1` through `.4`): timestamped PyCozmo protocol-level send/receive packet types, IDs and lengths, as well as its own protocol messages. Payloads are **not** included. "Sent" means the packet was queued by PyCozmo for transmission, not independently confirmed received or acknowledged by Cozmo; compare its timing to the transport counters in an incident to determine where communication stalled.

Only the high-volume `protocol.log` rotates automatically (approximately 40 MiB total); **disconnect incidents are retained indefinitely across service restarts**. Existing older `incidents.jsonl.1`–`.9` files from earlier installations are preserved as-is. Monitor available disk space if leaving this running for years. Protocol logging is buffered on a dedicated, bounded queue; it never blocks Cozmo's packet callbacks on file I/O. Recent protocol metadata is additionally kept in memory and synchronously written into a disconnect snapshot, including before a forced service restart. TTS contents, Wi-Fi passwords, connection URLs and complete MRR message payloads are not intentionally recorded. **These logs are still sensitive operational data** (timings, activity, and device state); review before sharing. Set `COZMO_LOG_DIR` on the rover service to change the directory. The stock PyCozmo sender, receiver and animation methods are not modified for logging.

To examine the latest failure, use `sudo tail -n 1 /var/log/cozmo-roverd/incidents.jsonl | python3 -m json.tool`; for the preceding packet activity use `sudo tail -n 150 /var/log/cozmo-roverd/protocol.log`.

## Exposure control in MRR

**Camera auto exposure is now the default** for a new configuration. This uses Cozmo firmware's native `SetCameraParams(auto_exposure_enabled=True)`, so the camera adjusts continuously when moving between indoor light and direct sunlight. Existing `/etc/cozmo-roverd.toml` files are deliberately not overwritten on upgrades: if yours still specifies `camera_mode = "bright"`, set **Camera exposure = Automatic** in the Fedora setup page and save to keep it active after restarts.

MRR's **Cozmo** peripheral now includes **Auto exposure** (momentary button: clicking enables native AE immediately) and **Manual exposure (ms)** (1–67 slider: dragging switches to minimum sensor gain with a specific exposure). The existing lift slider remains unchanged. These controls use MRR's existing supported peripheral schema; no MRR server modification is needed. Exposure modes persist across hardware reconnects within a bridge session. The Fedora diagnostic JSON reports `camera_exposure.mode` and `camera_exposure.manual_ms`. An ACK means the command was accepted for transmission, **not** proof the sensor exposure changed; verify with the live image.

## Isolated physical audio and light diagnostics

The real-hardware probe runs **outside the production bridge**, with a fresh, unmodified PyCozmo client in a separate child process for each case. No lights or audio are enabled during normal driving. Running these tests **can hang or disconnect Cozmo**, so the user must explicitly opt in. Do not run while the production service is active.

After installing/updating the bridge and preparing Cozmo's Wi-Fi, stop the rover and run cases individually:

```sh
sudo systemctl stop cozmo-roverd.service
sudo /opt/cozmo-roverd/.venv/bin/python -m cozmo_roverd.hardware_tests --case baseline-camera --i-understand-risk
sudo /opt/cozmo-roverd/.venv/bin/python -m cozmo_roverd.hardware_tests --case backpack-side-camera --i-understand-risk
sudo /opt/cozmo-roverd/.venv/bin/python -m cozmo_roverd.hardware_tests --case audio-camera --i-understand-risk
sudo systemctl start cozmo-roverd.service
```

Available cases: `baseline-idle` (no camera/animation), `baseline-camera`, `backpack-stock` (upstream `set_all_backpack_lights`), `backpack-side`, `backpack-side-camera`, `charger` (only if detected), `audio-idle` (upstream `play_audio`, 0.28-second quiet generated WAV), and `audio-camera`. The baseline and paired camera/no-camera cases distinguish a bad individual packet from interference from continuous camera/animation traffic.

**Each test runs in its own killable process**, samples reliable ACK-window sequence/queue counters four times per second, and records PyCozmo protocol messages and the actual playback-frame count. A sustained ACK stall writes an incident snapshot and aborts that case. The parent kills a worker that hangs during Wi-Fi connection or teardown. Results are retained under `/var/log/cozmo-roverd/hardware-tests/<timestamp>-<case>/`: `measurements.jsonl`, `incidents.jsonl`, and `protocol.log`. These are actual physical experiments, not tests that claim to prove hardware behavior using mocks. The test runner and packet formats also have automated unit tests, but **the real-world LED/audio result cannot be established until it is run on the robot**.

## TTS safety: hardware speaker disabled

**Hardware TTS is deliberately disabled in the normal Cozmo bridge.** Sending PyCozmo `OutputAudio` repeatedly stalled the reliable control channel, often forcing a robot disconnect. The reference audio example working in isolation was not sufficient evidence that it could work safely in a continuously controlled rover. Further experiments must be performed in a separate, isolated test and not silently re-enabled on this bridge.

MRR `tts` commands still update the **last message on Cozmo's face immediately**. If the request asks Cozmo to actually speak, the bridge returns an explicit unsupported-audio error rather than synthesizing a file, pausing video, transmitting speaker packets, or risking the robot. The `speak:false` option displays the message without an error. Camera streaming is no longer modified by TTS requests. Drive, servo and other controls are unaffected. The dashboard reports the disabled speaker status.

## Face stability and diagnostics

The face shows only battery voltage and the last TTS message. It renders directly at the native 128x32, 1-bit resolution so that even Pillow's built-in fallback font remains visible when Fedora's system TrueType fonts are missing. The previous 4x supersampled fallback could collapse the text into tiny dots. PyCozmo's encoded bitmap is decoded in-process to report lit pixel and occupied-column counts before the image is submitted.

The normal PyCozmo animation loop is left unmodified but disabled for this display-only bridge. The application periodically sends a `DisplayImage` when face content changes (or after 15 seconds to preserve it) and `OutputSilence` at most 4 Hz while a face is active, subject to ACK congestion checks. No audio packets are submitted. Display correctness and congestion checks are covered by tests; behavior on the physical robot still requires verification.

## Recovering an interrupted Cozmo connection

The bridge retains its MRR registration while reconnecting Cozmo. It now reuses an active NetworkManager association, waits through brief RobotState pauses, and retries fresh PyCozmo handshakes without transmitting a protocol Disconnect to an absent robot. A stalled PyCozmo start/stop triggers a supervised service restart rather than blocking recovery indefinitely. In the setup dashboard, **Cozmo recovery stage**, **Last Cozmo failure**, and **Robot reconnect attempts** explain what the bridge is doing.

Cozmo's physical 128×64 OLED uses an interleaved 128×32 image protocol; only alternating physical scan lines light at a time by design, to reduce burn-in. The face now shows **only battery voltage and the most recent TTS message**, with large type and horizontal scrolling for long speech text. No speed indicators, battery percentage, charging markers, eyes, or miscellaneous status are drawn. Incoming TTS text is submitted to the face immediately, before speech synthesis or playback; this prevents an audio failure from hiding the message. The bridge sends the **full 128-pixel-wide** generated dashboard whenever it fits within a single Cozmo network frame. The former 160-byte cap was incorrect and reduced the image to a small battery-only graphic on the left. A compact image is now used **only** if the encoded full dashboard exceeds the actual single-frame network payload limit. Face updates remain limited to once every five seconds and are deferred while the reliable control channel is busy or TTS is active. `face_last_encoded_bytes` and `face_last_error` show the current behavior.

FFmpeg's RTSP/encoding errors are written to `/run/cozmo-roverd/ffmpeg.log`, not endlessly printed in the bridge's journal. Use `journalctl -u cozmo-roverd -n 100 --no-pager` for connection messages and `cat /run/cozmo-roverd/ffmpeg.log` only when debugging video. This does not prove the physical cause of a Wi-Fi/robot disconnect; it makes recovery and failures observable.

## In-video HUD, camera field of view, and temporary disconnects

The optional **In-video telemetry HUD** places two small indicators at the top center of the existing 320×240 camera frame: the IMU ball (gravity-derived roll/pitch from Cozmo's accelerometer) and a narrow lift-position gauge immediately to its right. The original tiny hinged-arm gauge rotates with Cozmo's real lift position, with no text or numbers. Firmware 2381 has been observed reporting the lift angle in radians in a packet field named `lift_height_mm`; the bridge converts this using Cozmo's lift-arm geometry before displaying the lift height in the web UI or positioning the video indicator. Missing readings use a muted arm. Neither gauge adds a background panel, another video stream, extra frame queue, or an encoding pass. Disable the HUD in the Fedora setup UI.

The bridge uses the **whole frame reported by PyCozmo** (4:3), without applying a crop or digital zoom. PyCozmo also reports camera-calibrated horizontal and vertical field of view in the live telemetry data, when the robot provides it. Those are lens properties: raising resolution or stretching an image cannot physically widen the lens.

The MRR **rover WebSocket now remains connected even if Cozmo temporarily disconnects** from its dedicated Wi-Fi. The hardware task retries independently; MRR still receives periodic `roverState` updates with `drivingMode: false` until the robot reconnects, and motion commands receive an explicit temporary-unavailable acknowledgement rather than silently disappearing. When PyCozmo reconnects, the same MRR registration resumes control automatically without re-registering the rover. When camera frames stop, the RTSP publisher keeps the stream alive with a **live recovery slate** instead of a generic no-video message. It displays a centered **COZMO RECONNECTING** message with animated dots and a second line reporting the live supervisor operation: connecting to robot Wi-Fi, connecting to robot, closing the previous connection, waiting to retry, or waiting for camera frames. MRR status, counters, errors, and diagnostic panels are excluded. The slate updates during recovery even with the regular HUD switched off, and the next fresh camera frame restores live video without restarting RTSP. A genuine server-side WebSocket/network disconnect still requires reconnection and registration.

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
