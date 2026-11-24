# Cozmo RoverD

Standalone Fedora Linux bridge that makes an Anki Cozmo appear as a rover on an existing [MultiRoombaRover](https://github.com/legop3/MultiRoombaRover) server. **It does not install or modify anything in the MRR repository or on the server.** It uses [PyCozmo](https://github.com/zayfod/pycozmo) and the current MRR rover WebSocket protocol.

## Hardware/network

Use **two network paths**: an Ethernet connection or primary Wi-Fi for reaching MRR, and a **second, dedicated Wi-Fi adapter** for Cozmo. Cozmo provides its own Wi-Fi access point. This bridge binds a NetworkManager connection exclusively to the adapter chosen in config, disables the default route on that connection and uses it only to communicate with Cozmo. Do **not** choose the host's normal uplink adapter. The SSID and password must be read from Cozmo's screen.

## Fedora installation and operations dashboard

Plug a **second Wi-Fi adapter** into your Fedora computer. Keep Ethernet or your primary Wi-Fi connected to the MRR network. The setup UI explicitly protects adapters used for default routes.

```bash
git clone https://github.com/legop3/cozmo-roverd.git
cd cozmo-roverd
sudo bash install-fedora.sh
```

**From another computer on the network, open `http://FEDORA-HOST-IP:8765`.** The web interface is designed for headless servers: it listens on all interfaces and runs separately from the bridge, even when Cozmo is offline. No SSH tunnel, login, or HTTPS setup required. The installer opens TCP 8765 through active firewalld installations.

The setup page is a utilitarian **five-page operations console**, not a setup
wizard or a long scrolling form. **Overview** shows connection health, battery,
dock contact, Wi-Fi channel, camera rate and bridge Start/Stop. **Connections**
holds the dedicated Cozmo adapter, optional network discovery and MRR server
address. **Dock automation** contains the channel target and Home Assistant
dock switch. **Rover & video** groups identity, drive limits and camera
controls. **Diagnostics** holds detailed sensor/protocol measurements, raw
JSON and service logs. Moving between pages preserves unsaved edits; Save
appears only when settings change and restarts the bridge. The disconnected
video slate reports shutdown, power cycling, Wi-Fi reconnection and channel
verification when a reroll is running.

The setup UI validates data, keeps existing passwords when appropriate, writes `/etc/cozmo-roverd.toml` atomically with 0600 permissions and maintains a `.bak` copy, enables the rover service, and restarts it with the new settings. Service controls are on Overview; diagnostics and logs are separate. The dashboard uses server-measured telemetry freshness to avoid presenting old robot data as live.

To upgrade an existing installation, `git pull` then rerun `sudo bash install-fedora.sh`. Existing configuration is preserved. You do **not** have to manually edit the TOML. The web UI uses the same configuration file as the daemon.

The setup page listens on `0.0.0.0:8765` and does not change MRR itself. **There is no authentication or encryption**: anyone who can reach this port can change robot networking and restart the service. Keep port 8765 on a trusted LAN and do not forward it onto the internet.

## Optional Home Assistant dock-only Wi-Fi channel reroll

Cozmo's firmware may choose Wi-Fi channel 1, 6, or 11 at boot. Enable
**Automatic channel reroll** in the operations dashboard if you want a specific
channel. Select 1, 6, or 11, specify a Home Assistant base URL, a long-lived
access token, and an entity such as `switch.cozmo_dock` whose power output
physically supplies Cozmo's charger. No USB hub power controls are involved.

Use **Test Home Assistant switch** to validate the API token and read the
entity's state without toggling power. This only checks the Home Assistant
state, not the physical power path. Test that the selected switch really cuts
and restores charger power separately before enabling automation.

MRR registration and the RTSP publisher start independently of Cozmo and
remain available during Wi-Fi channel selection. The hardware supervisor
opens a **telemetry-only PyCozmo inspection connection**: the camera,
normal controls, LEDs, automatic lift maintenance and face updates are not
started. It reads the associated channel with `iw dev <interface> link` and
uses fresh RobotState charger-contact telemetry captured during the initial
handshake. If the channel matches, roverd **promotes that same connection**
to normal operation, starts the camera, and allows MRR commands.

If the channel is wrong, the robot must be stationary on the dock.
RoverD sends `ShutdownRobot`, closes the inspection socket, cycles the
dock power using Home Assistant, reconnects over Wi-Fi and inspects again.
The RTSP stream displays the appropriate status slate throughout; the
video does not briefly go live before another reroll. Unavailable Home
Assistant or missing charger-contact confirmation cannot trigger a shutdown.
The existing 10-minute MRR offline grace is retained as a rover-list cleanup
policy, not a limit on retrying.

- Unknown channel, unavailable switch, missing charger contacts or moving
  Cozmo: no shutdown or power cycle.
- During a reroll, MRR commands are withheld and the continuous RTSP stream
  displays a readable status slate. The dashboard and telemetry JSON report
  current/target channel, step, attempts, errors and next retry.
- Wrong-channel selections trigger another reroll automatically until Cozmo
  chooses the configured channel. Only operational errors back off 60 seconds;
  there is no artificial attempt cap or cooldown for a healthy reroll.
- The HA power controller always attempts `turn_on` after `turn_off`, and
  waits for Home Assistant to report state transitions. However, an HA
  outage, service termination, or an optimistic switch entity can prevent
  physical power restoration. For unattended use, configure an independent
  Home Assistant failsafe that restores dock power after a short outage.

**Disabled by default.** Existing installations behave normally until
all HA settings are saved and the automation is explicitly enabled. HA
credentials live in `/etc/cozmo-roverd.toml` (0600), never in public setup
responses. The setup dashboard itself serves HTTP without authentication:
use it only on a trusted LAN or protect it with a secure reverse proxy.

## Controls and integration

The **Private rover** toggle maps to MRR's native `private.enabled` property. It does not change access to the independent LAN setup dashboard.

- **Drive:** MRR's `driveDirect` sets continuous wheel speeds (no command-silence timeout). PyCozmo's published maximum is 200 mm/s, but Anki's original remote-control SDK example sends 250 mm/s commands. The bridge now offers 250 mm/s (default on new installs); existing configuration is preserved on upgrades, so select 250 in the dashboard if yours is still set lower. Telemetry records configured cap, commanded wheel speeds, and measured wheel speeds to establish what the robot actually achieves.
- **Head/camera and lights:** MRR controls the camera/head and its separate front illuminator. On each Cozmo connection, the five backpack LEDs get steady colors (red outer LEDs, blue/green/amber RGB center LEDs). The two outer LEDs are physically red-only. The physical charger LEDs use three simple steady states: **green when docked, red when undocked, purple during dock assist**. No blinking, charging-state effects, MRR-connection light changes, or manual backpack-light configuration. LEDs update only when needed and defer when the reliable transport is congested. The independent front headlight remains manually controllable.
- **Video quality:** Cozmo's color camera feed is already compressed and internally half-width (160×240 before pycozmo scales it to 320×240). RoverD now encodes that stream at high H.264 quality (libx264 CRF 17, veryfast, zerolatency; explicit controlled bitrate for OpenH264/VAAPI) instead of using FFmpeg encoder defaults. This prevents avoidable additional compression artifacts, but cannot recover sensor/firmware detail absent from the source.
- **Camera exposure:** Fully automatic sensor adjustment across sunlight and dim rooms. The bridge samples camera brightness and adjusts Cozmo's native exposure time (1–67 ms) and analog gain (0.1–3.984×), not RTSP pixel brightness. The former fixed bright/lowlight setup presets are migrated to automatic on load; the UI exposes no manual mode. Adjustments are sent outside the camera receive thread and only when needed.
- **Face status:** Cozmo's native 128×32 display uses adaptive, large TTS text on the left and a separate battery panel on the right (actual measured voltage, estimated four-segment charge bar and charging marker). Short messages grow to fill the panel; longer ones paginate without shrinking to microtext. The screen uses direct, bounded PyCozmo protocol packets; a low-rate animation-clock tick keeps the image visible. No custom PyCozmo animation methods are installed.
- **Lift:** Both the generic lift-height slider and the Roomba-shaped **main brush** control are supported. Signed main-brush PWM from -127 to 127 drives Cozmo's lift continuously down/up; zero stops the lift. Other brush/vacuum channels have no hardware equivalent and are ignored.
- **Audio:** Hardware TTS is disabled because playback never worked reliably and caused ACK stalls. MRR TTS text can still appear on Cozmo's display; speech requests return a clear unsupported-audio error.
- **Cliff:** Cozmo exposes one physical underside cliff detector, not four directional Roomba detectors. Its detected/not-detected flag is projected into MRR's *front-left* cliff channel as a compatibility mapping; the four raw samples are retained in the bridge's full telemetry. We deliberately do not invent other three detector readings. Cliff detection is telemetry-only: both the firmware cliff-stop and bridge-level cliff motor stop are disabled, so the sensor cannot interrupt or reject driving. Pickup, orientation, charger-contact and cliff flags are all telemetry-only: none can reject, throttle or stop manual drive commands. The bridge advertises disabled MRR private-safety overrides for this rover.
- **Docking:** `undock` automatically drives Cozmo **forward** off the charging contacts until contact clears, with a 3-second operation limit, and returns MRR's asynchronous `commandResult`. `dockAssist` prepares a user-guided approach; manual driving remains under direct MRR command even if dock contact occurs. `cancelDocking` exits assisted docking. **Automatic charger-seeking** is not yet implemented: without charger localization it would be unreliable, so `autoDock` explicitly reports unsupported instead of pretending to dock.
- **Battery:** Cozmo exposes voltage, charger status, and wheel activity, **not** a calibrated battery percentage or current. The bridge estimates battery state of charge from resting-voltage observations, learns bounded motor-voltage sag, limits corrections while driving, and treats charging voltage as biased (no immediate jump to 100%). The estimate and learned sag are periodically saved in `/var/log/cozmo-roverd/battery-gauge.json` and a recent state can be reused after a restart. Raw voltage is still available; this is an approximate observer, not a hardware fuel gauge or a capacity measurement. Gauge mode, filtered voltage and learned sag are exposed in battery diagnostics.

On an MRR connection loss or daemon shutdown, motion is stopped. No stale driving command is replayed after reconnect.

## Video

Camera images are published via FFmpeg to the server's existing MediaMTX RTSP endpoint, default `rtsp://SERVER:8554/<rover.name>`. Resolution: 320x240, color, up to 15 FPS, H.264 low-latency settings. Camera presets use PyCozmo's native SetCameraParams exposure/gain control: **Auto**, **Bright**, or **Low light**, with gain/exposure clamped to the robot's reported camera limits. Software H.264 is preferred at this small resolution; VA-API is only attempted if neither software encoder works. The server must accept RTSP publishes. If the default MediaMTX RTSP port is not 8554, configure `media.rtsp_url` explicitly.

**Note:** Fedora's `ffmpeg-free` build may not contain `libx264`. The bridge automatically uses `libopenh264` when available instead. Enable the official `fedora-cisco-openh264` repository and install `openh264` if necessary, or install a full FFmpeg build. Check `ffmpeg -encoders | grep -E 'libx264|libopenh264'`. The installer does not silently enable third-party repositories.

## PyCozmo packet framing fix

The bridge uses **upstream PyCozmo unchanged at runtime**: no send-thread or animation-controller method replacements. The unused experimental UDP frame-batching patch and failed animation/audio monkey-patch have been removed. Since upstream's animation controller would continuously enqueue display and silence frames, this bridge disables animation playback and sends its static face image directly as a standard `DisplayImage` packet with a limited `OutputSilence` clock (up to 4 Hz) and an image refresh when needed. The face path checks the reliable send window before enqueueing traffic. This preserves the existing HUD, camera, driving, and other control paths without invasive patches.

## Frame pacing and receive-thread isolation

Live PyCozmo callbacks now hand off camera images immediately to the RTSP
publisher and queue at most one image for automatic exposure analysis (sampled
at most every 0.8 seconds). `RobotState` packets likewise move onto a bounded
single-item worker queue: stale backlogged packets are replaced by the latest
state rather than blocking PyCozmo's receive thread. Dock contacts, battery
recovery, and MRR state updates retain the same handling on that worker.
Maintenance calls for auto-exposure, dock lift, and lighting run on independent
cadences; slow optional work no longer holds up the telemetry export loop.
No face or hardware audio traffic has been re-enabled.

The local `/run/cozmo-roverd/telemetry.json` now exposes video diagnostics:
`video.camera_input_fps_2s` (actual incoming Cozmo camera frames),
`video.rtsp_output_fps_2s` (frames successfully written to FFmpeg),
`video.rtsp_unique_camera_frames`, `video.rtsp_repeated_camera_frames`,
`video.ffmpeg_write_last_ms`, and `video.ffmpeg_write_max_ms`.
It also includes `state_packets_coalesced` and `camera_analysis_skipped`.
If video stutters, compare input FPS against output FPS and write latency
before assuming the robot or network is dropping frames.

## Persistent reconnect diagnostics

The bridge writes **persistent, size-bounded diagnostics** to `/var/log/cozmo-roverd/`, independently of the systemd journal. These survive a service restart or a robot disconnect:

- `incidents.jsonl`: **append-only, never automatically rotated or deleted**, containing structured JSON events for Wi-Fi/robot/MRR connection stages and full disconnect snapshots **before** PyCozmo is cleaned up. Each snapshot contains the last 120 bridge events (including recent commanded wheel speeds and lift/head actions), 90 seconds of sampled reliable-ACK/send-window/RobotState/camera metrics, and metadata for the last 300 PyCozmo packet send/receive events.
- `protocol.log` (plus `.1` through `.4`): timestamped PyCozmo protocol-level send/receive packet types, IDs and lengths, as well as its own protocol messages. Payloads are **not** included. "Sent" means the packet was queued by PyCozmo for transmission, not independently confirmed received or acknowledged by Cozmo; compare its timing to the transport counters in an incident to determine where communication stalled.

Only the high-volume `protocol.log` rotates automatically (approximately 40 MiB total); **disconnect incidents are retained indefinitely across service restarts**. Existing older `incidents.jsonl.1`–`.9` files from earlier installations are preserved as-is. Monitor available disk space if leaving this running for years. Protocol logging is buffered on a dedicated, bounded queue; it never blocks Cozmo's packet callbacks on file I/O. Recent protocol metadata is additionally kept in memory and synchronously written into a disconnect snapshot, including before a forced service restart. TTS contents, Wi-Fi passwords, connection URLs and complete MRR message payloads are not intentionally recorded. **These logs are still sensitive operational data** (timings, activity, and device state); review before sharing. Set `COZMO_LOG_DIR` on the rover service to change the directory. The stock PyCozmo sender, receiver and animation methods are not modified for logging.

To examine the latest failure, use `sudo tail -n 1 /var/log/cozmo-roverd/incidents.jsonl | python3 -m json.tool`; for the preceding packet activity use `sudo tail -n 150 /var/log/cozmo-roverd/protocol.log`.

## Reconnect Cozmo from MRR

MRR's **Cozmo** peripheral exposes the existing **Lift height (%)** slider and
a momentary **Reconnect Cozmo** button. The previous manual/automatic exposure
accessory controls have been removed; automatic camera exposure still runs
internally and is not changed by this button.

Clicking **Reconnect Cozmo** requests a single controlled reconnection from
the hardware supervisor. It marks the hardware temporarily unavailable,
stops the existing PyCozmo client, then reconnects via the existing dedicated
Wi-Fi association. It does not create a second client, restart the whole
RoverD service, or intentionally disconnect the MRR WebSocket. RTSP shows
the existing reconnect screen until fresh camera images return. The command
ACK acknowledges the reconnect request, not completion of the handshake.
The button is unavailable while the hardware is disconnected because MRR's
command receiver rejects hardware actions until it reconnects.

## Pinned pycozmo UDP frame-split fix

Audio tests exposed a real upstream `pycozmo.conn.SendThread._send_packets` off-by-one bug: when a large `OutputAudio` packet causes a UDP frame split, the previous frame's header incorrectly uses the **next packet's** sequence number. This can prevent the robot from acknowledging the frame. The corrected implementation uses the sequence number of the final packet actually present in the outgoing frame.

The Fedora installer now builds pycozmo from upstream **commit `26b23c099c835fc86bcf9b7f454eb1110ec07488`**, checks and applies `patches/pycozmo-udp-frame-sequence.patch` *to the dependency source before installing*, then verifies the installed module. The same install step runs in CI; an actual UDP frame-encode/decode regression test covers large audio packets that split into separate datagrams. This is a **source-level dependency patch, not a runtime monkeypatch**. It is kept in this repo because the connected GitHub integration does not have access to create or write a maintained fork of upstream. Future pycozmo updates are intentionally pinned until the patch is reviewed.

After pulling the change, run `sudo ./install-fedora.sh` to install the corrected dependency. `git pull` alone will not change the copy of pycozmo in the virtual environment. The hardware-test runner verifies the installed fix before it runs; it will not misleadingly claim to test the fix while using unpatched pycozmo. **Passing regression tests proves correct frame encoding, not that Cozmo's actual audio hardware now works**; the physical tests still need repeating.

## Real-hardware audio and lighting diagnostics

The diagnostic suite runs as one command, **stops the rover service if it is running, restores it afterward**, and prints the actual experimental evidence directly to the terminal: a timestamped command and failure timeline, ACK-window progression, send-queue and pending-window growth, incoming frame/packet counters, audio playback counters, and the most recent transmitted packet types and lengths. PASS/FAIL is only a heading, not the report. The tests use stock PyCozmo in isolated worker processes. The full detailed evidence remains on disk for later comparisons.

From your existing checkout on the Fedora host (after `git pull`), run:

```sh
sudo env PYTHONPATH="$PWD" /opt/cozmo-roverd/.venv/bin/python -m cozmo_roverd.hardware_tests --suite --i-understand-risk
```

**Using `PYTHONPATH="$PWD"` deliberately runs the current checkout** rather than a potentially outdated copy installed into the Python virtual environment. The runner detects and temporarily stops `cozmo-roverd.service` so it does not compete for the robot. It restores the original running service after testing, including if a test fails. If the service was already stopped, it leaves it stopped.

The suite runs five comparisons: camera baseline, stock animation+camera baseline, side LEDs+camera, stock audio without camera, and stock audio+camera. Use `--case CASE` instead of `--suite` to run just one, e.g. `--case baseline-animation-camera`. Other optional cases: `baseline-idle`, `baseline-animation`, `backpack-stock`, `backpack-side`, and `charger`.

These are genuine physical commands, and **they may hang the robot's firmware**. Each worker has a watchdog and persists the last available measurements. Each case makes up to three fresh PyCozmo connections for transient handshake timeouts; a case that never connects is explicitly **INCONCLUSIVE**, never counted as evidence of a feature failure.

In addition to the existing command and playback counters, the isolated test records **actual socket-level UDP frame headers** (transmit/receive direction, timestamps, sequence first/last, ACK number, packet types/lengths, and repeated sequence ranges), checks declared sequence spans against the number of reliable packets in each frame, and observes exceptions otherwise swallowed by PyCozmo's `SendThread.run`. Its observer forwards original socket bytes unchanged; it does not change `_send_packets`, ACK handling, retransmissions, or production transport. The terminal output prints the first malformed frames, send-thread exceptions, final outbound/inbound UDP frame headers and the ACK/queue timeline. Only metadata is retained, not image or audio payloads. Detailed results remain in `/var/log/cozmo-roverd/hardware-tests/<timestamp>-diagnostics/` including `wire-frames.jsonl`. The runner does not require opening or transferring these files to see the evidence.

## TTS safety: hardware speaker disabled

**Hardware TTS is deliberately disabled in the normal Cozmo bridge.** Sending PyCozmo `OutputAudio` repeatedly stalled the reliable control channel, often forcing a robot disconnect. The reference audio example working in isolation was not sufficient evidence that it could work safely in a continuously controlled rover. Further experiments must be performed in a separate, isolated test and not silently re-enabled on this bridge.

MRR `tts` commands still update the **last message on Cozmo's face immediately**. If the request asks Cozmo to actually speak, the bridge returns an explicit unsupported-audio error rather than synthesizing a file, pausing video, transmitting speaker packets, or risking the robot. The `speak:false` option displays the message without an error. Camera streaming is no longer modified by TTS requests. Drive, servo and other controls are unaffected. The dashboard reports the disabled speaker status.

## Cozmo low-battery charging recovery

MRR continues to own locking and unlocking; this bridge reports
`battery.needsCharge`. Cozmo has no current sensor or calibrated charge
percentage, so the displayed estimate is **not** a prerequisite for unlocking.
After a confirmed low-battery event, recovery now requires either:

- **15 minutes of received charging-state telemetry** with a recent valid
  battery reading of at least **3.65 V**, or
- **35 minutes of received charging-state telemetry** if Cozmo withholds
  valid voltage samples while docked, or
- An explicitly detected completed charge cycle following at least
  two minutes of observed charging, with adequate recent voltage.

Only successive fresh charging packets accumulate credit; a silent robot,
stale telemetry, dock contact without charging, and pauses in charging cannot
earn unobserved time. Brief pauses retain accrued credit; losing contact for
30 seconds or a long telemetry gap resets it. Recent readings below 3.45 V
block unlocking. When recovery is confirmed, RoverD also clears its old
low-voltage latch so undocking does not immediately trigger another lock.
All recovery evidence is diagnostic-only and no new commands are sent to Cozmo.

## MRR presence after a Cozmo hardware outage

Cozmo stays listed on MRR for **10 minutes** after the robot hardware
connection fails, while the bridge keeps retrying PyCozmo independently.
During this grace period, the RTSP reconnecting slate displays
`MRR OFFLINE IN MM:SS`. If the robot reconnects, the countdown disappears
and resets completely. If it remains disconnected for 10 uninterrupted
minutes, the bridge closes only its MRR WebSocket so the rover goes offline
in the server's normal presence system. The bridge and RTSP worker keep
running, and MRR registration resumes automatically when Cozmo reconnects.
The 10-minute timer also applies on bridge startup when no robot is available.
No MRR server changes are necessary.

## Temporary face transport isolation

Hardware logs on November 22 showed fresh camera and RobotState packets but
no progress on reliable outbound acknowledgments. Automatic `DisplayImage`
and `OutputSilence` traffic is therefore disabled by default to isolate
that suspect path and restore priority to motor commands. The face bitmap
renderer and its explicit opt-in unit tests remain, but Cozmo's physical face
will not receive dashboard updates in this safety build. The original
in-video IMU and lift indicators are unchanged. This is an isolation step,
not a proven root-cause fix.

## Face stability and diagnostics

The face shows dynamically sized TTS text alongside a separated battery panel with raw measured voltage and an explicitly estimated charge bar. It renders directly at the native 128x32, 1-bit resolution. The Fedora installer supplies scalable DejaVu fonts so large text is readable even on headless systems; a built-in font is retained as a fallback. The previous 4x supersampled fallback could collapse the text into tiny dots. PyCozmo's encoded bitmap is decoded in-process to report lit pixel and occupied-column counts before the image is submitted.

The normal PyCozmo animation loop is left unmodified but disabled for this display-only bridge. The application periodically sends a `DisplayImage` when face content changes (or after 15 seconds to preserve it) and `OutputSilence` at most 4 Hz while a face is active, subject to ACK congestion checks. No audio packets are submitted. Display correctness and congestion checks are covered by tests; behavior on the physical robot still requires verification.

## Recovering an interrupted Cozmo connection

The bridge retains its MRR registration while reconnecting Cozmo. It now reuses an active NetworkManager association, waits through brief RobotState pauses, and retries fresh PyCozmo handshakes without transmitting a protocol Disconnect to an absent robot. A stalled PyCozmo start/stop triggers a supervised service restart rather than blocking recovery indefinitely. In the setup dashboard, **Cozmo recovery stage**, **Last Cozmo failure**, and **Robot reconnect attempts** explain what the bridge is doing.

Cozmo's physical 128×64 OLED uses an interleaved 128×32 image protocol; only alternating physical scan lines light at a time by design, to reduce burn-in. The face shows the most recent TTS message in dynamically fitted large text, with readable pages for long messages. Its dedicated side panel shows **actual battery voltage, an approximate charge bar, and a charging marker**. No speed indicators, eyes, or miscellaneous robot status are drawn. Incoming TTS text is submitted to the face immediately, before speech synthesis or playback; this prevents an audio failure from hiding the message. The bridge sends the **full 128-pixel-wide** generated dashboard whenever it fits within a single Cozmo network frame. The former 160-byte cap was incorrect and reduced the image to a small battery-only graphic on the left. A compact image is now used **only** if the encoded full dashboard exceeds the actual single-frame network payload limit. Face updates remain limited to once every five seconds and are deferred while the reliable control channel is busy or TTS is active. `face_last_encoded_bytes` and `face_last_error` show the current behavior.

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
