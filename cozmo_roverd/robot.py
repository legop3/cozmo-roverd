"""PyCozmo hardware access; isolate robot callbacks from the asyncio WebSocket."""
from __future__ import annotations
import logging
import subprocess
import tempfile
from pathlib import Path
import math
import threading
import time
from PIL import Image, ImageDraw
from .face import status_image, compact_status_image
from .status_lights import charger_lights, charger_signature
from .telemetry import snapshot
from .robot_packet import BatteryTracker, snapshot_packet
LOG = logging.getLogger(__name__)

class Robot:
    def __init__(self, *, max_speed, on_state, on_sensor, on_frame, camera_mode="bright"):
        self.max_speed = max_speed
        self.camera_mode = camera_mode
        self.docking_phase = None
        self.dock_assist = False
        self.undock_active = False
        self._undock_cancel = threading.Event()
        self._undock_thread = None
        self.on_state, self.on_sensor, self.on_frame = on_state, on_sensor, on_frame
        self.client = None
        self.lock = threading.RLock()
        self.charging = None
        self.on_charger = None
        self.cliff = None
        self.picked_up = False
        self.latest = {}
        self.headlight = False
        self.voltage = None
        self.last_sensor = 0.0
        self.moving = False
        self.commanded_wheels = (0, 0)
        self.ready = False
        self.battery = BatteryTracker()
        self.packets_received = 0
        self.last_packet_error = None
        self.packet_status = {}
        self.last_tts = ""
        self.last_tts_error = "Hardware speech disabled for robot stability"
        self._tts_transport_faulted = True
        self._tts_last_stream_at = 0.0
        self.last_tts_frames = 0
        self.last_tts_played_at = None
        self.face_frames_sent = 0
        self._face_scroll = 0
        self._face_last_content = None
        self._face_sent_at = 0.0
        self._face_clock_at = 0.0
        self.face_last_encoded_bytes = None
        self.face_last_error = None
        self.face_encoded_on_pixels = None
        self.face_decoded_columns = None
        self._status_light_signature = None
        self._charger_factory_id = None
        self._charger_object_id = None
        self._charger_connect_at = 0.0
        self._charger_last_signature = None
        self._charger_last_attempt = 0.0

    def connect(self):
        import pycozmo
        # Keep the stock PyCozmo transport and animation internals intact.
        # We do not use its 30Hz animation/audio loop: the bridge has no
        # supported hardware speaker, and it publishes only a static face.
        # Face packets are sent directly using the public connection API.
        cli = pycozmo.Client(enable_animations=False, enable_procedural_face=False)
        cli.start()
        try:
            cli.connect()
            cli.wait_for_robot(timeout=12)
            cli.add_handler(pycozmo.event.EvtNewRawCameraImage,
                            lambda _client, image: self.on_frame(image))
            cli.add_handler(pycozmo.event.EvtRobotChargingChange,
                            lambda _client, value: self._charging(bool(value)))
            cli.add_handler(pycozmo.event.EvtCliffDetectedChange,
                            lambda _client, value: self._cliff(bool(value)))
            cli.add_handler(pycozmo.event.EvtRobotPickedUpChange,
                            lambda _client, value: self._picked_up(bool(value)))
            # Handle actual packets, retaining all direct fields. Registered
            # after PyCozmo's own handler so its high-level state is current.
            cli.add_handler(pycozmo.protocol_encoder.RobotState,
                            lambda _client, pkt: self._packet(cli, pkt))
            cli.enable_camera(enable=True, color=True)
            # Do not enable speaker playback or alter camera streaming
            # in this stable driver. The last requested TTS remains visible.
            cli.enable_animations(False)
            cli.enable_procedural_face(False)
            cli.conn.send(pycozmo.protocol_encoder.EnableStopOnCliff(enable=False))
            self.configure_camera(cli)
            with self.lock:
                self.client = cli
                self.last_sensor = time.monotonic()
                self.ready = True
                # The new robot display has no cached image from the old link.
                self._face_last_content = None
                self._face_sent_at = 0.0
                self._face_clock_at = 0.0
                self._status_light_signature = None
                self._charger_factory_id = None
                self._charger_object_id = None
                self._charger_connect_at = 0.0
                self._charger_last_signature = None
                self._charger_last_attempt = 0.0
            self.on_state()
            LOG.info("Cozmo connected")
        except Exception:
            # On a failed handshake the robot may already be absent. Do not
            # enqueue a protocol Disconnect into an unacknowledged socket.
            # Reclaim local PyCozmo threads and let the supervisor retry.
            try:
                cli.stop()
            except Exception:
                LOG.exception("Could not close failed PyCozmo connection")
            raise

    def _charging(self, value):
        self.charging = value
        self.on_state()

    def _cliff(self, value):
        # Cliff sensing remains telemetry-only; it must not change motor commands.
        self.cliff = value

    def _picked_up(self, value):
        # Pickup is telemetry only, not a driving permission check.
        self.picked_up = value

    def _packet(self, cli, pkt):
        # No network I/O in the PyCozmo receive thread.
        self.packets_received += 1
        now = time.monotonic()
        self.last_sensor = now
        try:
            data = snapshot_packet(pkt)
            battery = self.battery.observe(pkt.battery_voltage, pkt.status, now)
            old = (self.on_charger, self.charging, self.voltage, self.cliff,
                   self.picked_up)
            self.voltage = battery.voltage
            self.on_charger = battery.docked
            self.charging = battery.charging
            self.cliff = data["cliff_detected"]
            self.picked_up = data["picked_up"]
            # Rich PyCozmo-derived values are best effort and must never
            # prevent the actual packet and battery state reaching MRR.
            try:
                rich = snapshot(cli, pkt)
                rich.update(data)
                data = rich
            except Exception as exc:
                self.last_packet_error = str(exc)[:160]
            data["battery_tracking"] = battery.diagnostic()
            data["packet_count"] = self.packets_received
            self.latest = data
            if self.dock_assist and self.on_charger:
                # The charger flag is an observation, not permission to
                # override a driver command. End the assist status only.
                self.dock_assist = False
                self.docking_phase = None
            changed = old != (self.on_charger, self.charging, self.voltage,
                             self.cliff, self.picked_up)
            if changed:
                self.on_state()
        except Exception as exc:
            self.last_packet_error = str(exc)[:160]
            LOG.exception("Cozmo RobotState packet handling failed")
        # Always keep the packet receive counter available for diagnostics.

    def is_healthy(self):
        if not self.ready:
            return False
        # Charger status arrives in RobotState packets, but those packets
        # need not remain frequent throughout charging. The Wi-Fi link is
        # checked separately by the supervisor. Do not force a reconnect
        # solely for missing telemetry while on charging contacts.
        if self.battery.docked is True:
            return True
        return time.monotonic() - self.last_sensor < 5.0

    def stop_drive(self):
        with self.lock:
            if self.client is not None:
                try: self.client.drive_wheels(0, 0)
                except Exception: LOG.exception("Unable to stop wheels")
            self.moving = False
            self.commanded_wheels = (0, 0)

    def drive(self, left, right):
        with self.lock:
            if self.client is None: raise RuntimeError("Cozmo disconnected")
            # Request a rapid hardware transition, not a software ramp.
            # High acceleration is explicit; no command timeout is introduced.
            self.client.drive_wheels(left, right, lwheel_acc=10000.0, rwheel_acc=10000.0)
            self.moving = left != 0 or right != 0
            self.commanded_wheels = (left, right)

    def lift_motor(self, main):
        """Use MRR main-brush signed PWM to control the lift continuously."""
        if type(main) is not int or not -127 <= main <= 127:
            raise ValueError("main brush PWM must be -127..127")
        with self.lock:
            if self.client is None:
                raise RuntimeError("Cozmo disconnected")
            self.client.move_lift(2.0 * main / 127.0)

    def configure_camera(self, cli=None):
        import pycozmo
        with self.lock:
            client = cli or self.client
            if client is None:
                raise RuntimeError("Cozmo disconnected")
            presets = {"auto": (1.0, 0, True),
                       "bright": (4.0, 40, False),
                       "lowlight": (6.0, 60, False)}
            gain, exposure, automatic = presets[self.camera_mode]
            if not automatic and client.camera_config is not None:
                c = client.camera_config
                gain = max(c.min_gain, min(c.max_gain, gain))
                exposure = max(c.min_exposure_time_ms,
                               min(c.max_exposure_time_ms, exposure))
            client.conn.send(pycozmo.protocol_encoder.SetCameraParams(
                gain=float(gain), exposure_ms=int(exposure),
                auto_exposure_enabled=automatic))
            LOG.info("Camera mode %s: gain %.2f, exposure %dms, auto=%s",
                     self.camera_mode, gain, exposure, automatic)

    def start_undock(self, command_id, backoff, completed):
        """Drive forward off Cozmo charging contacts, then report completion."""
        with self.lock:
            if self.undock_active:
                raise ValueError("undock already in progress")
            if self.client is None or not self.ready:
                raise RuntimeError("Cozmo disconnected")
            if self.on_charger is not True:
                completed(command_id, None)
                return
            self.undock_active = True
            self.docking_phase = "undocking"
            self.dock_assist = False
            self._undock_cancel.clear()
        self.on_state()

        def worker():
            error = None
            try:
                if backoff:
                    self.drive(self.max_speed, self.max_speed)
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        if self._undock_cancel.is_set():
                            raise RuntimeError("undock interrupted")
                        if self.on_charger is False:
                            break
                        time.sleep(0.05)
                    if self.on_charger is not False:
                        raise RuntimeError("Charger contact did not clear")
            except Exception as exc:
                error = str(exc)
            finally:
                self.stop_drive()
                with self.lock:
                    self.undock_active = False
                    self.docking_phase = None
                self.on_state()
                completed(command_id, error)

        self._undock_thread = threading.Thread(
            target=worker, name="cozmo-undock", daemon=True)
        self._undock_thread.start()

    def interrupt_undock(self):
        """A new drive command overrides a scripted undock immediately."""
        self._undock_cancel.set()
        thread = self._undock_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
            if thread.is_alive():
                raise RuntimeError("Unable to interrupt undock operation")
        self.undock_active = False

    def assist_docking(self):
        with self.lock:
            if self.client is None:
                raise RuntimeError("Cozmo disconnected")
            if self.on_charger:
                return
            self.dock_assist = True
            self.docking_phase = "assisting"
            self.stop_drive()
            self.head(-25)
        self.on_state()

    def cancel_docking(self):
        with self.lock:
            if self.undock_active:
                return
            self.dock_assist = False
            self.docking_phase = None
            self.stop_drive()
            self.head(10)
        self.on_state()

    def head(self, degrees):
        with self.lock:
            if self.client is None: raise RuntimeError("Cozmo disconnected")
            self.client.set_head_angle(math.radians(max(-25, min(44, degrees))))

    def lift(self, percent):
        with self.lock:
            if self.client is None: raise RuntimeError("Cozmo disconnected")
            # Cozmo's robot lift height is in millimeters.
            self.client.set_lift_height(32.0 + (92.0 - 32.0) * percent / 100.0)

    def light(self, enabled):
        """Control Cozmo's actual head light, the MRR headlight role."""
        with self.lock:
            if self.client is None: raise RuntimeError("Cozmo disconnected")
            self.client.set_head_light(enabled)
            self.headlight = enabled
        self.on_state()

    def refresh_status_lights(self, mrr_connected=False):
        """Send one minimal side-LED packet only when link status changes.

        Previous implementation sent both LightStateCenter and LightStateSide,
        including firmware-paced color animations. During the new lighting
        rollout Cozmo stopped ACKing commands. Start with the supported
        red-only side LEDs and avoid touching the center LEDs entirely.
        """
        with self.lock:
            if self.client is None or not self.ready:
                return False
            import pycozmo
            signature = bool(mrr_connected)
            if signature == self._status_light_signature:
                return False
            if not self._lights_transport_ready(self.client):
                return False
            # The side LEDs have only red emitters. Two steady states are
            # unambiguous and require no animation timing on the firmware.
            red = pycozmo.lights.red_light
            off = pycozmo.lights.off_light
            self.client.conn.send(
                pycozmo.protocol_encoder.LightStateSide(
                    states=(red, red if mrr_connected else off)))
            self._status_light_signature = signature
            return True

    def refresh_charger_lights(self):
        """Connect to the real charger and update its lights when status changes.

        Uses the official pycozmo ObjectConnect -> ObjectConnectionState ->
        CubeId -> CubeLights protocol. Never block on wait_for() and never
        mistake an ordinary cube for the charger. Missing/unpowered docks are
        optional: just retry discovery later.
        """
        with self.lock:
            cli = self.client
            if cli is None or not self.ready:
                return False
            import pycozmo
            p = pycozmo.protocol_encoder
            now = time.monotonic()
            available = getattr(cli, "available_objects", {})
            connected = getattr(cli, "connected_objects", {})
            # Discovery can arrive after the robot connects. Restrict every
            # lookup to Charger_Basic so cubes never inherit dock effects.
            charger_ids = {
                factory_id for factory_id, obj in list(available.items())
                if getattr(obj, "object_type", None) == p.ObjectType.Charger_Basic
            }
            candidates = [
                (object_id, data.get("factory_id"))
                for object_id, data in list(connected.items())
                if data.get("object_type") == p.ObjectType.Charger_Basic
            ]
            if candidates:
                object_id, factory_id = candidates[0]
                if object_id != self._charger_object_id:
                    self._charger_last_signature = None
                self._charger_object_id = object_id
                self._charger_factory_id = factory_id
            else:
                self._charger_object_id = None
                self._charger_last_signature = None
                if self._charger_factory_id not in charger_ids:
                    self._charger_factory_id = None
                    self._charger_connect_at = 0.0
                # Existing pending requests get a chance to complete before
                # another attempt; missing chargers are retried sparingly.
                if not charger_ids or now - self._charger_last_attempt < 15.0:
                    return False
                factory_id = (self._charger_factory_id
                              if self._charger_factory_id in charger_ids
                              else next(iter(charger_ids)))
                self._charger_factory_id = factory_id
                if not self._lights_transport_ready(cli):
                    return False
                cli.conn.send(p.ObjectConnect(factory_id=factory_id, connect=True))
                self._charger_connect_at = now
                self._charger_last_attempt = now
                return True

            signature = charger_signature(
                docked=self.battery.docked, charging=self.battery.charging,
                docking=bool(self.dock_assist or self.undock_active))
            if signature == self._charger_last_signature:
                return False
            if not self._lights_transport_ready(cli):
                return False
            # CubeId selects the charger, CubeLights changes that object's LEDs.
            cli.conn.send(p.CubeId(object_id=self._charger_object_id))
            cli.conn.send(p.CubeLights(states=charger_lights(
                docked=self.battery.docked, charging=self.battery.charging,
                docking=bool(self.dock_assist or self.undock_active))))
            self._charger_last_signature = signature
            return True

    @staticmethod
    def _lights_transport_ready(cli):
        """Avoid adding reliable packets during ACK or send-queue congestion."""
        sender = getattr(getattr(cli, "conn", None), "send_thread", None)
        if sender is None:
            return True
        with sender.lock:
            pending = ((sender.window.next_seq - sender.window.expected_seq)
                       % sender.window.max_seq)
        return pending < 6 and sender.queue.qsize() < 6

    def transport_diagnostics(self):
        """Read PyCozmo's reliable packet window without changing it."""
        cli = self.client
        if cli is None:
            return None
        sender = cli.conn.send_thread
        with sender.lock:
            window = sender.window
            pending = (window.next_seq - window.expected_seq) % window.max_seq
            expected_seq = window.expected_seq
        return {
            "pending_reliable_packets": pending,
            "send_window_size": window.size,
            "next_ack_expected": expected_seq,
            "outgoing_packets": sender.outgoing_packets,
            "transmitted_packets_including_retries": sender.sent_packets,
            "pending_application_commands": sender.queue.qsize(),
            "received_frames": cli.conn.recv_thread.received_frames,
        }

    def speaker_diagnostics(self):
        """Distinguish TTS synthesis/queueing from actual playback reports."""
        with self.lock:
            cli = self.client
            played = (getattr(cli.anim_controller, "num_audio_frames_played", None)
                      if cli else None)
            queue_size = None
            if cli is not None:
                q = cli.anim_controller.queue
                with q.lock:
                    queue_size = len(q.audio_queue)
            return {
                "last_message": self.last_tts,
                "last_error": self.last_tts_error,
                "last_queued_frames": self.last_tts_frames,
                "last_queued_at": self.last_tts_played_at,
                "robot_reported_audio_frames": played,
                "queued_frames_remaining": queue_size,
                "speaker_disabled_after_transport_failure": self._tts_transport_faulted,
                "hardware_speech_enabled": False,
            }

    def note_tts(self, text):
        """Remember a speech message even if synthesis or playback fails."""
        message = " ".join(str(text).split())[:512]
        if not message:
            return
        with self.lock:
            self.last_tts = message
            self.last_tts_error = "Hardware speech disabled for robot stability"
            self._face_scroll = 0
            self._face_last_content = None
            self._face_sent_at = 0.0
        # Show the newly requested text *before* speech synthesis and audio
        # transfer begin. Otherwise a stuck TTS queue suppresses the face
        # refresh, so the most recent message never becomes visible.
        # refresh_face retains normal connection/backlog safeguards.
        self.refresh_face()

    def refresh_face(self):
        """Refresh the Cozmo face dashboard, observing bandwidth limits."""
        with self.lock:
            if self.client is None or not self.ready:
                return False
            # The firmware stores DisplayImage as an animation keyframe,
            # not just a UDP payload. Refresh slowly to avoid its small
            # animation-keyframe ring filling while video is active.
            if time.monotonic() - self._face_sent_at < 5.0:
                return False
            # Display updates are lower priority than actual wheel/audio
            # commands. When the reliable send window fills, adding a new
            # image only increases retransmissions and blocks controls.
            sender = getattr(getattr(self.client, "conn", None), "send_thread", None)
            if sender is not None:
                with sender.lock:
                    pending = (sender.window.next_seq - sender.window.expected_seq) % sender.window.max_seq
                if pending >= 8 or sender.queue.qsize() >= 8:
                    return False
            battery = self.battery
            image = status_image(
                voltage=battery.voltage,
                last_tts=self.last_tts,
                scroll=self._face_scroll,
            )
            # Use the full-width dashboard whenever the encoded image fits
            # in a single protocol frame. The former arbitrary 160-byte
            # limit silently replaced it with a tiny battery-only icon at
            # the left edge, making the screen look broken. Cozmo's
            # animation-buffer warnings describe its *available* space at
            # that instant, not a 160-byte maximum image format.
            # Sending is already deferred during TTS and transport congestion.
            from pycozmo.image_encoder import ImageEncoder
            from pycozmo.protocol_declaration import MAX_FRAME_PAYLOAD_SIZE
            max_image_size = MAX_FRAME_PAYLOAD_SIZE - 6
            encoded = bytes(ImageEncoder(image).encode())
            if len(encoded) > max_image_size:
                image = compact_status_image(
                    voltage=battery.voltage, last_tts=self.last_tts,
                    scroll=self._face_scroll)
                encoded = bytes(ImageEncoder(image).encode())
            if len(encoded) > max_image_size:
                image = compact_status_image(
                    voltage=battery.voltage, last_tts=self.last_tts,
                    scroll=self._face_scroll, message_limit=8)
                encoded = bytes(ImageEncoder(image).encode())
            if len(encoded) > max_image_size:
                image = compact_status_image(
                    voltage=battery.voltage, battery_only=True)
                encoded = bytes(ImageEncoder(image).encode())
            if len(encoded) > max_image_size:
                self.face_last_error = (
                    f"Cannot fit face keyframe into {max_image_size} bytes "
                    f"(smallest generated image is {len(encoded)})")
                LOG.warning("%s", self.face_last_error)
                return False
            # Confirm the actual PyCozmo wire encoder retains the entire
            # 128-column bitmap. The robot showing a single dot despite
            # a full rendered image points to animation transport rather
            # than font/layout, and these counts separate those failures.
            from pycozmo.image_encoder import ImageDecoder
            decoded = ImageDecoder(encoded).decode()
            foreground = sum(bool(px) for px in decoded)
            columns = sum(
                any(decoded[y * 128 + x] for y in range(32))
                for x in range(128))
            self.face_encoded_on_pixels = foreground
            self.face_decoded_columns = columns
            if foreground == 0 or columns < 8:
                self.face_last_error = (
                    f"Encoded bitmap unexpectedly sparse: {foreground} pixels, "
                    f"{columns} occupied columns")
                LOG.warning("%s", self.face_last_error)
                return False
            self.face_last_encoded_bytes = len(encoded)
            content = image.tobytes()
            self._face_scroll += 1
            # The stock animation controller is disabled. Its
            # Client.display_image() merely enqueues a frame for that
            # controller, so send the same supported DisplayImage packet
            # directly rather than monkey-patching the controller.
            # Refresh the unchanged bitmap at most once every 15 seconds
            # because Cozmo expires displayed animation images.
            if (content == self._face_last_content
                    and time.monotonic() - self._face_sent_at < 15.0):
                return False
            conn = getattr(self.client, "conn", None)
            if conn is not None and callable(getattr(conn, "send", None)):
                from pycozmo.protocol_encoder import DisplayImage
                conn.send(DisplayImage(image=encoded))
            else:
                # Test doubles without a transport can still inspect pixels.
                self.client.display_image(image)
            self.face_frames_sent += 1
            self._face_sent_at = time.monotonic()
            self._face_last_content = content
            self.face_last_error = None
            return True

    def advance_face_display(self):
        """Keep the face animation clock moving without replacing PyCozmo.

        Cozmo needs occasional OutputSilence ticks to display a static
        DisplayImage. This is isolated application traffic, not a custom
        animation-thread implementation, and never sends hardware speech.
        """
        with self.lock:
            if not self.ready or self.client is None or self._face_last_content is None:
                return False
            now = time.monotonic()
            if now - self._face_clock_at < 0.25:
                return False
            conn = getattr(self.client, "conn", None)
            if conn is None or not callable(getattr(conn, "send", None)):
                return False
            sender = getattr(conn, "send_thread", None)
            if sender is not None:
                with sender.lock:
                    pending = ((sender.window.next_seq - sender.window.expected_seq)
                               % sender.window.max_seq)
                if pending >= 8 or sender.queue.qsize() >= 8:
                    return False
            from pycozmo.protocol_encoder import OutputSilence
            conn.send(OutputSilence())
            self._face_clock_at = now
            return True

    def mark_speech_unstable(self, reason):
        """Disable speaker output until service restart after transport failure.

        A failed OutputAudio can freeze Cozmo's reliable command channel.
        Keep the rover usable and retain latest message text; do not retry
        the same dangerous packet repeatedly on every new TTS request.
        """
        with self.lock:
            self._tts_transport_faulted = True
            self.last_tts_error = str(reason)[:180]
        LOG.error("Cozmo speaker disabled for this service session: %s", reason)

    def speak(self, text, *, speed=1.0):
        """Hardware speech is intentionally disabled after repeated crashes."""
        self.last_tts_error = "Hardware speech disabled for robot stability"
        raise RuntimeError(self.last_tts_error)


    def close(self):
        """Release the robot promptly without network writes on a dead link.

        Never hold the command lock while joining PyCozmo worker threads:
        callbacks may need the same lock to finish, causing recovery to hang.
        When the robot has disappeared, sending motor commands and a protocol
        disconnect is both ineffective and unnecessary. Closing the local
        socket and threads is sufficient before a fresh connection attempt.
        """
        with self.lock:
            cli, self.client = self.client, None
            self.ready = False
            self.moving = False
            self.commanded_wheels = (0, 0)
            # Preserve the most recent *measured* battery voltage and
            # charging history across PyCozmo reconnects. Cozmo may not
            # send another valid voltage immediately while docked; resetting
            # the tracker caused the OLED/MRR to show "--.-- V" even
            # though we'd already measured the battery moments earlier.
            # Tracker freshness remains stale until a new RobotState packet.
            self._undock_cancel.set()
            self.undock_active = False
            self.docking_phase = None
            self.dock_assist = False
            self._status_light_signature = None
            self._charger_factory_id = None
            self._charger_object_id = None
            self._charger_connect_at = 0.0
            self._charger_last_signature = None
            self._charger_last_attempt = 0.0
            self.latest = {}
            self.voltage = self.battery.voltage
            self.on_charger = self.battery.docked
            self.charging = self.battery.charging
            self.cliff = None
        if cli is None:
            return
        LOG.info("Closing local PyCozmo client for reconnection")
        try:
            cli.stop()
        except Exception:
            LOG.exception("Could not close PyCozmo client")
