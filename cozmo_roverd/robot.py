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

def _limit_animation_idle_traffic(cli):
    """Keep audio frames at native cadence; do not flood Cozmo while idle.

    PyCozmo's default AnimationController sends OutputSilence and the most
    recently displayed image on *every* 30 Hz animation tick. With camera
    packets already streaming that is a substantial constant transmit load,
    and can contribute to transport congestion or firmware buffer starvation.
    Drive it with its normal timer/queue but send only changed images, one
    ending silence packet after speech, and a screen keepalive every 10 s.
    """
    import types
    from pycozmo import event, protocol_encoder

    controller = cli.anim_controller
    controller._cozmo_image_refresh_at = 0.0

    def run_frame(self):
        audio_pkt, image_pkt, pkts = self.queue.get()
        if not self.animations_enabled:
            return
        if audio_pkt is not None:
            self.cli.conn.send(audio_pkt)
            self.playing_audio = True
        elif self.playing_audio:
            self.playing_audio = False
            self.cli.conn.send(protocol_encoder.OutputSilence())
            self.cli.conn.post_event(event.EvtAudioCompleted, self.cli)
        if (image_pkt is None and self.procedural_face_enabled
                and not self.playing_animation and not self.image_displayed):
            image_pkt = self._get_face_image()
        if image_pkt is not None:
            self.last_image_pkt = image_pkt
            self.cli.conn.send(image_pkt)
            self._cozmo_image_refresh_at = time.monotonic()
        elif (self.image_displayed
              and time.monotonic() - self._cozmo_image_refresh_at >= 10.0):
            # Cozmo otherwise times out its display at around 30 seconds.
            self.cli.conn.send(self.last_image_pkt)
            self._cozmo_image_refresh_at = time.monotonic()
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
        self.last_tts_error = None
        self.last_tts_frames = 0
        self.last_tts_played_at = None
        self.face_frames_sent = 0
        self._face_scroll = 0
        self._face_last_content = None
        self._face_sent_at = 0.0
        self.face_last_encoded_bytes = None
        self.face_last_error = None

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
            cli.set_volume(50000)
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
            played = getattr(cli, "num_audio_frames_played", None) if cli else None
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
            }

    def note_tts(self, text):
        """Remember a speech message even if synthesis or playback fails."""
        message = " ".join(str(text).split())[:512]
        if not message:
            return
        with self.lock:
            self.last_tts = message
            self.last_tts_error = None
            self._face_scroll = 0
            self._face_last_content = None
            self._face_sent_at = 0.0

    def refresh_face(self):
        """Update robot-local diagnostics once per second, not per camera frame."""
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
            if controller is not None and getattr(controller, "playing_audio", False):
                return False
            battery = self.battery
            image = status_image(
                voltage=battery.voltage,
                percent=battery.percent,
                charging=battery.charging,
                docked=battery.docked,
                cliff=self.cliff, picked_up=self.picked_up,
                moving=self.moving, wheels=self.commanded_wheels,
                last_tts=self.last_tts, audio_error=self.last_tts_error,
                scroll=self._face_scroll,
            )
            # A legal UDP packet can still overrun Cozmo's much smaller
            # animation *keyframe* buffer. Logs show keyframes of 280-370
            # bytes being rejected even with 69-298 bytes remaining.
            # Prefer the full graphic only when compact enough; otherwise
            # switch to a designed minimal bitmap. Never enqueue a known
            # oversized frame and jam the reliable command channel.
            from pycozmo.image_encoder import ImageEncoder
            from pycozmo.protocol_declaration import MAX_FRAME_PAYLOAD_SIZE
            max_image_size = min(160, MAX_FRAME_PAYLOAD_SIZE - 6)
            encoded = bytes(ImageEncoder(image).encode())
            if len(encoded) > max_image_size:
                image = compact_status_image(
                    voltage=battery.voltage, percent=battery.percent,
                    charging=battery.charging, last_tts=self.last_tts,
                    scroll=self._face_scroll)
                encoded = bytes(ImageEncoder(image).encode())
            if len(encoded) > max_image_size:
                image = compact_status_image(
                    voltage=battery.voltage, percent=battery.percent,
                    charging=battery.charging, last_tts=self.last_tts,
                    scroll=self._face_scroll, message_limit=8)
                encoded = bytes(ImageEncoder(image).encode())
            if len(encoded) > max_image_size:
                image = compact_status_image(
                    voltage=battery.voltage, percent=battery.percent,
                    charging=battery.charging, battery_only=True)
                encoded = bytes(ImageEncoder(image).encode())
            if len(encoded) > max_image_size:
                self.face_last_error = (
                    f"Cannot fit face keyframe into {max_image_size} bytes "
                    f"(smallest generated image is {len(encoded)})")
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

    def speak(self, text, *, speed=1.0):
        """Generate eSpeak audio and queue validated frames to Cozmo's speaker."""
        import pycozmo
        message = " ".join(str(text).split())[:512]
        if not message:
            raise ValueError("TTS text is empty")
        speed = max(0.5, min(2.0, float(speed)))
        try:
            with tempfile.TemporaryDirectory(prefix="cozmo-speech-") as directory:
                raw = str(Path(directory) / "raw.wav")
                output = str(Path(directory) / "cozmo.wav")
                subprocess.run(
                    ["espeak-ng", "-s", str(round(175 * speed)), "-w", raw, message],
                    check=True, capture_output=True, timeout=20)
                subprocess.run(
                    ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                     "-i", raw, "-ar", "22050", "-ac", "1",
                     "-c:a", "pcm_s16le", output],
                    check=True, capture_output=True, timeout=20)
                if not Path(output).is_file() or Path(output).stat().st_size <= 44:
                    raise RuntimeError("TTS produced no PCM audio")
                frames = pycozmo.audio.load_wav(output)
                if not frames:
                    raise RuntimeError("TTS produced no Cozmo audio frames")
            with self.lock:
                if self.client is None or not self.ready:
                    raise RuntimeError("Cozmo disconnected")
                controller = self.client.anim_controller
                controller.enable_animations(True)
                # Explicitly use the same 30-FPS output-audio queue that the
                # built-in PyCozmo playback uses, with a verified nonempty WAV.
                controller.play_audio(frames)
                self.last_tts_frames = len(frames)
                self.last_tts_played_at = time.time()
                self.last_tts_error = None
            LOG.info("Queued %d Cozmo speaker frames for TTS", len(frames))
        except (OSError, subprocess.CalledProcessError,
                subprocess.TimeoutExpired, ValueError, RuntimeError) as exc:
            with self.lock:
                self.last_tts_error = str(exc)[:180]
            LOG.exception("Cozmo speaker TTS failed")
            raise RuntimeError(f"Cozmo TTS failed: {exc}") from exc

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
