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
from .telemetry import snapshot
from .robot_packet import BatteryTracker, snapshot_packet
LOG = logging.getLogger(__name__)

def _fix_pycozmo_packet_batching(cli):
    """Fix PyCozmo's wrong last sequence when a UDP frame overflows.

    Its stock SendThread._send_packets() uses the *next* packet's sequence
    for the frame being flushed. The recipient can then acknowledge a
    sequence that was not included in that frame. Audio packets (744 bytes)
    make the overflow branch especially common.
    """
    import types
    from pycozmo.protocol_declaration import MAX_FRAME_PAYLOAD_SIZE

    sender = cli.conn.send_thread

    def send_packets(self, pkts, last_ack):
        to_frame = []
        frame_len = 0
        first_seq = last_seq = None
        for seq, pkt in pkts:
            pktlen = 4 + len(pkt)
            if pktlen >= MAX_FRAME_PAYLOAD_SIZE:
                raise ValueError(
                    f"PyCozmo packet {type(pkt).__name__} ({pktlen} bytes) "
                    "exceeds single UDP frame payload")
            if to_frame and frame_len + pktlen >= MAX_FRAME_PAYLOAD_SIZE:
                self._send_frame(to_frame, first_seq, last_seq, last_ack)
                to_frame = []
                frame_len = 0
            if not to_frame:
                first_seq = seq
            to_frame.append(pkt)
            frame_len += pktlen
            last_seq = seq
        if to_frame:
            self._send_frame(to_frame, first_seq, last_seq, last_ack)

    sender._send_packets = types.MethodType(send_packets, sender)


def _limit_animation_idle_traffic(cli):
    """Stable, display-only PyCozmo animation loop.

    The optional speaker path is disabled: it repeatedly froze the reliable
    Cozmo channel. Do not patch audio pacing or touch the camera here.
    A new bitmap is sent once, with low-rate OutputSilence frames to advance
    Cozmo's animation/display subsystem. Keep the last bitmap alive every
    10 seconds. No unconditional 30 Hz image or silence flooding.
    """
    import types
    from pycozmo import protocol_encoder

    controller = cli.anim_controller
    controller._cozmo_image_refresh_at = 0.0
    controller._cozmo_last_idle_silence_at = 0.0
    controller._cozmo_audio_aborted = False
    controller._cozmo_audio_abort_reason = "Hardware TTS disabled for stability"
    controller._cozmo_audio_backpressure_ticks = 0
    controller._cozmo_audio_frames_submitted = 0
    controller._cozmo_audio_played_baseline = None
    controller._cozmo_keyframes_received = 0
    controller._cozmo_keyframe_gating = False
    controller._cozmo_camera_paused_for_speech = False
    controller._cozmo_tts_camera_restores = 0

    def run_frame(self):
        if not self.animations_enabled:
            return
        now = time.monotonic()
        # TTS must not be sent, including anything accidentally queued.
        q = self.queue
        if hasattr(q, "lock") and hasattr(q, "audio_queue"):
            with q.lock:
                q.audio_queue.clear()
        _, image_pkt, pkts = q.get()
        sender = getattr(getattr(self.cli, "conn", None), "send_thread", None)
        pending, waiting = 0, 0
        if sender is not None:
            with sender.lock:
                pending = (
                    (sender.window.next_seq - sender.window.expected_seq)
                    % sender.window.max_seq)
            waiting = sender.queue.qsize()

        # Always accept a fresh operator status image unless the radio is
        # already seriously congested; avoid losing it from the image queue.
        if image_pkt is not None:
            if pending >= 12 or waiting >= 8:
                if hasattr(q, "image_queue") and hasattr(q, "lock"):
                    with q.lock:
                        q.image_queue.appendleft(image_pkt)
            else:
                self.last_image_pkt = image_pkt
                self.cli.conn.send(image_pkt)
                self._cozmo_image_refresh_at = now

        # PyCozmo normally sends OutputSilence at 30 Hz; slow this to 4 Hz
        # while preserving the animation clock needed to show images.
        if (self.image_displayed and pending < 12 and waiting < 8
                and now - self._cozmo_last_idle_silence_at >= 0.25):
            self.cli.conn.send(protocol_encoder.OutputSilence())
            self._cozmo_last_idle_silence_at = now

        # Refresh only the last actual image, never the stock blank packet.
        if (image_pkt is None and self.image_displayed and pending < 8
                and waiting < 4 and
                now - self._cozmo_image_refresh_at >= 10.0):
            self.cli.conn.send(self.last_image_pkt)
            self._cozmo_image_refresh_at = now

        if pkts:
            for pkt in pkts:
                self.cli.conn.send(pkt)

    controller._run_frame = types.MethodType(run_frame, controller)


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
        self.face_last_encoded_bytes = None
        self.face_last_error = None
        self.face_encoded_on_pixels = None
        self.face_decoded_columns = None

    def connect(self):
        import pycozmo
        cli = pycozmo.Client(enable_animations=True, enable_procedural_face=False)
        _limit_animation_idle_traffic(cli)
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
            cli.enable_animations(True)
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

    def backpack(self, enabled):
        """Backpack LEDs are independent of the front-facing head light."""
        with self.lock:
            if self.client is None: raise RuntimeError("Cozmo disconnected")
            import pycozmo
            self.client.set_all_backpack_lights(
                pycozmo.lights.white_light if enabled else pycozmo.lights.off_light)

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
                "aborted_due_to_congestion": getattr(cli.anim_controller, "_cozmo_audio_aborted", False) if cli else None,
                "abort_reason": getattr(cli.anim_controller, "_cozmo_audio_abort_reason", None) if cli else None,
                "backpressure_ticks": getattr(cli.anim_controller, "_cozmo_audio_backpressure_ticks", 0) if cli else None,
                "frames_submitted_to_transport": getattr(cli.anim_controller, "_cozmo_audio_frames_submitted", 0) if cli else None,
                "playback_counter_at_speech_start": getattr(cli.anim_controller, "_cozmo_audio_played_baseline", None) if cli else None,
                "speaker_disabled_after_transport_failure": self._tts_transport_faulted,
                "camera_paused_for_speech": getattr(cli.anim_controller, "_cozmo_camera_paused_for_speech", False) if cli else None,
                "camera_restores_after_speech": getattr(cli.anim_controller, "_cozmo_tts_camera_restores", 0) if cli else None,
                "firmware_keyframes_received": getattr(cli.anim_controller, "_cozmo_keyframes_received", None) if cli else None,
                "keyframe_gating_enabled": getattr(cli.anim_controller, "_cozmo_keyframe_gating", None) if cli else None,
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
            controller = getattr(self.client, "anim_controller", None)
            if controller is not None:
                if getattr(controller, "playing_audio", False):
                    return False
                audio_queue = getattr(getattr(controller, "queue", None), "audio_queue", None)
                if audio_queue is not None and len(audio_queue):
                    return False
                if getattr(controller, "_cozmo_audio_aborted", False):
                    self.last_tts_error = "Speech aborted to protect Cozmo command channel"
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
            if content == self._face_last_content:
                return False
            self.client.display_image(image)
            self.face_frames_sent += 1
            self._face_sent_at = time.monotonic()
            self._face_last_content = content
            self.face_last_error = None
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
            self.battery = BatteryTracker()
            self._undock_cancel.set()
            self.undock_active = False
            self.docking_phase = None
            self.dock_assist = False
            self.latest = {}
            self.voltage = None
            self.on_charger = None
            self.charging = None
            self.cliff = None
        if cli is None:
            return
        LOG.info("Closing local PyCozmo client for reconnection")
        try:
            cli.stop()
        except Exception:
            LOG.exception("Could not close PyCozmo client")
