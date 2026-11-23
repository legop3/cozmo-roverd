"""PyCozmo hardware access; isolate robot callbacks from the asyncio WebSocket."""
from __future__ import annotations
import logging
import subprocess
import tempfile
from pathlib import Path
import math
import queue
import threading
import time
from PIL import Image, ImageDraw
from .face import bounded_status_pages
from .status_lights import backpack_lights, charger_lights, charger_signature
from .telemetry import snapshot
from .robot_packet import BatteryTracker, snapshot_packet
LOG = logging.getLogger(__name__)

class Robot:
    def __init__(self, *, max_speed, on_state, on_sensor, on_frame, camera_mode="auto", camera_enabled=True):
        self.max_speed = max_speed
        self.camera_mode = camera_mode
        self.camera_enabled = camera_enabled
        self.manual_exposure_ms = 10
        self._auto_gain = 1.0
        self._auto_exposure_ms = 16
        self._auto_last_measure_at = 0.0
        self._auto_pending = False
        self._auto_measured_brightness = None
        self.docking_phase = None
        self.dock_assist = False
        self._dock_light_owned = False
        self.undock_active = False
        self._undock_cancel = threading.Event()
        self._undock_thread = None
        self.on_state, self.on_sensor, self.on_frame = on_state, on_sensor, on_frame
        self.client = None
        self.lock = threading.RLock()
        self.charging = None
        self.on_charger = None
        self._dock_contact_handled = False
        # Only the last commanded automatic lift target is remembered.
        # Actual lift height in RobotState determines whether to retry.
        self._dock_lift_target = None
        self._dock_lift_sent_at = 0.0
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
        self._state_queue = queue.Queue(maxsize=1)
        self._state_worker = None
        self._state_stop = threading.Event()
        self._had_packet_workers = False
        self.state_packets_coalesced = 0
        self._camera_analysis_queue = queue.Queue(maxsize=1)
        self._camera_analysis_worker = None
        self._camera_analysis_stop = threading.Event()
        self._camera_analysis_last_queued_at = 0.0
        self.camera_analysis_skipped = 0
        # Cheap wire counters: show whether Cozmo is sending camera chunks
        # even when pycozmo cannot assemble complete JPEG images.
        self.camera_chunks_received = 0
        self.camera_last_chunk_at = None
        self.camera_images_completed = 0
        self.camera_last_image_at = None
        self.last_packet_error = None
        self.packet_status = {}
        self.last_tts = ""
        self.last_tts_error = "Hardware speech disabled for robot stability"
        self._tts_transport_faulted = True
        self._tts_last_stream_at = 0.0
        self.last_tts_frames = 0
        self.last_tts_played_at = None
        # Static face is enabled, but never run the 30 Hz animation sender.
        self._face_transport_enabled = False
        self._face_blocked_since = None
        self._face_suspended_until = 0.0
        self.face_frames_sent = 0
        self._face_scroll = 0
        self._face_last_content = None
        self._face_sent_at = 0.0
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
        # Keep PyCozmo's 30 Hz animation/audio loop off: it caused
        # repeated reconnects on the physical robot.
        cli = pycozmo.Client(enable_animations=False, enable_procedural_face=False)
        cli.start()
        try:
            cli.connect()
            cli.wait_for_robot(timeout=12)
            cli.add_handler(pycozmo.event.EvtNewRawCameraImage,
                            lambda _client, image: self._camera_image(image))
            cli.add_handler(pycozmo.protocol_encoder.ImageChunk,
                            lambda _client, pkt: self._camera_chunk(pkt))
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
            # Start bounded consumers before enabling the camera. The raw
            # protocol callback must never perform motor I/O or rich decoding.
            self._start_packet_workers()
            cli.enable_camera(enable=self.camera_enabled, color=True)
            # Do not enable speaker playback or alter camera streaming
            # in this stable driver. The last requested TTS remains visible.
            cli.enable_animations(False)
            cli.enable_procedural_face(False)
            cli.conn.send(pycozmo.protocol_encoder.EnableStopOnCliff(enable=False))
            if self.camera_enabled:
                self.configure_camera(cli)
            with self.lock:
                self.client = cli
                self.last_sensor = time.monotonic()
                self.ready = True
                # The new robot display has no cached image from the old link.
                self._face_last_content = None
                self._face_sent_at = 0.0
                self._face_blocked_since = None
                self._face_suspended_until = 0.0
                self._status_light_signature = None
                self._dock_contact_handled = False
                self._dock_lift_target = None
                self._dock_lift_sent_at = 0.0
                self._dock_light_owned = False
                self._auto_gain = 1.0
                self._auto_exposure_ms = 16
                self._auto_last_measure_at = 0.0
                self._auto_pending = False
                self._charger_factory_id = None
                self._charger_object_id = None
                self._charger_connect_at = 0.0
                self._charger_last_signature = None
                self._charger_last_attempt = 0.0
            self.on_state()
            LOG.info("Cozmo connected")
        except Exception:
            self._stop_packet_workers()
            # On a failed handshake the robot may already be absent. Do not
            # enqueue a protocol Disconnect into an unacknowledged socket.
            # Reclaim local PyCozmo threads and let the supervisor retry.
            try:
                cli.stop()
            except Exception:
                LOG.exception("Could not close failed PyCozmo connection")
            raise

    def _camera_chunk(self, pkt):
        """Observe incoming image traffic without decoding or copying it."""
        self.camera_chunks_received += 1
        self.camera_last_chunk_at = time.monotonic()

    def _camera_image(self, image):
        self.camera_images_completed += 1
        self.camera_last_image_at = time.monotonic()
        self.on_frame(image)

    def _charging(self, value):
        self.charging = value
        self.on_state()

    def _cliff(self, value):
        # Cliff sensing remains telemetry-only; it must not change motor commands.
        self.cliff = value

    def _picked_up(self, value):
        # Pickup is telemetry only, not a driving permission check.
        self.picked_up = value

    def _start_packet_workers(self):
        self._had_packet_workers = True
        self._state_stop.clear()
        self._camera_analysis_stop.clear()
        self._camera_analysis_last_queued_at = 0.0
        self._state_worker = threading.Thread(
            target=self._state_worker_loop, name="cozmo-state", daemon=True)
        self._camera_analysis_worker = threading.Thread(
            target=self._camera_analysis_loop, name="cozmo-camera-analysis", daemon=True)
        self._state_worker.start()
        self._camera_analysis_worker.start()

    @staticmethod
    def _put_latest(q, item):
        try:
            q.put_nowait(item)
            return False
        except queue.Full:
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(item)
            except queue.Full:
                pass
            return True

    def _state_worker_loop(self):
        while not self._state_stop.is_set():
            try:
                cli, pkt = self._state_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if not self._state_stop.is_set():
                self._process_packet(cli, pkt)

    def _camera_analysis_loop(self):
        while not self._camera_analysis_stop.is_set():
            try:
                image = self._camera_analysis_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if not self._camera_analysis_stop.is_set():
                try:
                    self.observe_camera_brightness(image)
                except Exception:
                    LOG.exception("Camera brightness sampling failed")

    def queue_camera_analysis(self, image):
        """Sample at most every 0.8s, without blocking the camera callback."""
        if self._camera_analysis_worker is None or not self._camera_analysis_worker.is_alive():
            return False
        now = time.monotonic()
        if now - self._camera_analysis_last_queued_at < 0.8:
            return False
        self._camera_analysis_last_queued_at = now
        if self._put_latest(self._camera_analysis_queue, image):
            self.camera_analysis_skipped += 1
        return True

    def _packet(self, cli, pkt):
        """Keep PyCozmo's receive callback bounded and nonblocking."""
        self.packets_received += 1
        self.last_sensor = time.monotonic()
        if self._state_stop.is_set() and self._had_packet_workers:
            return  # A late callback from a closed real client.
        if self._state_worker is not None and self._state_worker.is_alive():
            if self._put_latest(self._state_queue, (cli, pkt)):
                self.state_packets_coalesced += 1
        else:
            # Manual/fake clients in tests have no worker. A closed real
            # connection is excluded above by _state_stop.
            self._process_packet(cli, pkt)

    def _process_packet(self, cli, pkt):
        now = time.monotonic()
        try:
            data = snapshot_packet(pkt)
            battery = self.battery.observe(
                pkt.battery_voltage, pkt.status, now,
                moving=(abs(pkt.lwheel_speed_mmps) > 5
                        or abs(pkt.rwheel_speed_mmps) > 5
                        or any(abs(v) > 5 for v in self.commanded_wheels)))
            # Only control-relevant transitions should trigger full MRR
            # roverState updates. Raw voltage changes every hardware sample,
            # but MRR already receives bounded voltage sensor telemetry.
            old = (self.on_charger, self.charging, self.cliff,
                   self.picked_up, self.dock_assist)
            was_docked = self.on_charger
            self.voltage = battery.voltage
            self.on_charger = battery.docked
            self.charging = battery.charging
            # Physical contact stops the wheels once. Charger and assist
            # both demand lift UP, independent of previous commands.
            contact = battery.raw_docked or battery.raw_charging
            if (contact and not self._dock_contact_handled
                    and self.ready and self.client is not None):
                self._dock_contact_handled = True
                if not self.undock_active:
                    self.stop_drive()
                    self.set_dock_lift(True, force=True)
                    self._end_dock_assist(lower_lift=False)
                    self.head(0)
            elif not contact and battery.docked is False:
                self._dock_contact_handled = False
            if (was_docked is not False and self.on_charger is False
                    and not self.undock_active and self.client is not None):
                # Actual departure, including manually lifting Cozmo off.
                # Assist still active means keep lift up for the approach.
                if not self.dock_assist:
                    self.head(0)
                    self.set_dock_lift(False)
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
            changed = old != (self.on_charger, self.charging, self.cliff,
                             self.picked_up, self.dock_assist)
            if changed:
                self.on_state()
        except Exception as exc:
            self.last_packet_error = str(exc)[:160]
            LOG.exception("Cozmo RobotState packet handling failed")
        # Always keep the packet receive counter available for diagnostics.

    def _stop_packet_workers(self):
        self._state_stop.set()
        self._camera_analysis_stop.set()
        for worker in (self._state_worker, self._camera_analysis_worker):
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=2)
        self._state_worker = None
        self._camera_analysis_worker = None
        for q in (self._state_queue, self._camera_analysis_queue):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

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

    def set_dock_lift(self, up, *, force=False):
        """One automatic lift rule: assist or dock means up, otherwise down.

        The most recent automatic target only deduplicates commands.
        Actual measured height can cause a paced retry via refresh_dock_lift.
        """
        with self.lock:
            if self.client is None:
                return False
            target = 100 if up else 0
            if not force and target == self._dock_lift_target:
                return False
            self.lift(target)
            self._dock_lift_target = target
            self._dock_lift_sent_at = time.monotonic()
            return True

    def refresh_dock_lift(self):
        """Correct a missed lift-up command using measured physical height.

        Only enforce UP while parked/assisting; free driving can still use
        the lift slider. This is run outside the RobotState receive thread.
        """
        with self.lock:
            if self.client is None or not self.ready or self.undock_active:
                return False
            if not (self.dock_assist or self.on_charger is True):
                return False
            if self.on_charger and self.moving:
                return False  # manually leaving dock
            now = time.monotonic()
            if self._dock_lift_target != 100:
                return self.set_dock_lift(True)
            height = self.latest.get("lift_height_mm")
            if (height is None or self.battery.stale(now) or height >= 85
                    or now - self._dock_lift_sent_at < 2.0):
                return False
            return self.set_dock_lift(True, force=True)

    def drive(self, left, right):
        with self.lock:
            if self.client is None:
                raise RuntimeError("Cozmo disconnected")
            if self.dock_assist:
                left = max(-50, min(50, left))
                right = max(-50, min(50, right))
            # Manual movement from the dock counts as departure, even before
            # a new packet reports the contacts clearing.
            if ((left or right) and self.on_charger is True
                    and not self.undock_active):
                self._end_dock_assist(lower_lift=False)
                if self.set_dock_lift(False):
                    self.head(0)
            self.client.drive_wheels(
                left, right, lwheel_acc=10000.0, rwheel_acc=10000.0)
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
        """Apply real firmware auto exposure or a bounded manual setting.

        The AE flag belongs to SetCameraParams (packet 0x57), not to the
        image encoder. Make the mode switch immediate without restarting
        the camera, and keep it across robot reconnections.
        """
        import pycozmo
        with self.lock:
            client = cli or self.client
            if client is None:
                raise RuntimeError("Cozmo disconnected")
            config = client.camera_config
            if self.camera_mode == "auto":
                # Use our camera-feedback automatic exposure rather than the
                # firmware's AE, which doesn't cover the user's full lighting
                # range on this robot. Update only when measured brightness
                # changes, and always respect the calibrated sensor limits.
                gain, exposure, automatic = self._auto_gain, self._auto_exposure_ms, False
            elif self.camera_mode == "manual":
                # Do not pin sensor gain to its 0.1x minimum: that made
                # the manual shutter slider unusably dark compared to the
                # former low-light preset (which reached ~4x gain).
                gain, exposure, automatic = 2.0, self.manual_exposure_ms, False
            elif self.camera_mode == "bright":
                gain, exposure, automatic = 4.0, 40, False
            elif self.camera_mode == "lowlight":
                gain, exposure, automatic = 6.0, 60, False
            else:
                raise ValueError(f"Unknown camera mode {self.camera_mode!r}")
            if config is not None and not automatic:
                gain = max(config.min_gain, min(config.max_gain, gain))
                exposure = max(config.min_exposure_time_ms,
                               min(config.max_exposure_time_ms, exposure))
            client.conn.send(pycozmo.protocol_encoder.SetCameraParams(
                gain=float(gain), exposure_ms=int(exposure),
                auto_exposure_enabled=automatic))
            LOG.info("Camera settings applied: mode=%s gain=%.3f exposure=%sms auto=%s",
                     self.camera_mode, gain, exposure, automatic)
            return True

    def observe_camera_brightness(self, image):
        """Use real camera pixels to automatically adjust sensor exposure/gain.

        Sample at most once per 0.8 seconds and do no transport I/O from
        PyCozmo's image callback. Avoid tiny oscillations near the target.
        """
        now = time.monotonic()
        with self.lock:
            if (self.camera_mode != "auto" or self.client is None or not self.ready
                    or now - self._auto_last_measure_at < 0.8):
                return
            self._auto_last_measure_at = now
        # A tiny thumbnail is sufficient and avoids affecting video encoding.
        from PIL import Image
        sample = image.convert("L").resize((32, 24))
        hist = sample.histogram()
        pixels = sum(hist)
        if not pixels:
            return
        # Ignore the darkest/brightest 10% so a small lamp or black border
        # does not dominate brightness; operate on the central 80%.
        lower, upper = pixels * 0.1, pixels * 0.9
        count, total, rank = 0, 0, 0
        for value, n in enumerate(hist):
            used = max(0, min(rank + n, upper) - max(rank, lower))
            total += used * value
            count += used
            rank += n
        brightness = total / count if count else 0.0
        with self.lock:
            self._auto_measured_brightness = round(brightness, 1)
            # Brightness near 105 is left alone to prevent flicker.
            if 88 <= brightness <= 125:
                return
            # Use gentle exposure changes; long exposure when necessary,
            # and analog gain for dim scenes. No post-processing.
            factor = max(0.55, min(1.8, 105 / max(1, brightness)))
            current = self._auto_exposure_ms * self._auto_gain
            target = current * factor
            conf = getattr(self.client, "camera_config", None)
            max_gain = float(getattr(conf, "max_gain", 3.984375))
            min_gain = float(getattr(conf, "min_gain", 0.1))
            max_ms = int(getattr(conf, "max_exposure_time_ms", 67))
            min_ms = int(getattr(conf, "min_exposure_time_ms", 1))
            target = max(min_ms * min_gain, min(max_ms * max_gain, target))
            # Favor shutter up to 33ms (30fps), then increase gain, then
            # extend shutter further when already at max gain.
            new_exposure = max(min_ms, min(33, max_ms, round(target)))
            new_gain = max(min_gain, min(max_gain, target / new_exposure))
            if new_gain >= max_gain and target > new_exposure * max_gain:
                new_exposure = min(max_ms, max(min_ms, round(target / max_gain)))
                new_gain = max_gain
            if (abs(new_exposure - self._auto_exposure_ms) >= 1
                    or abs(new_gain - self._auto_gain) >= 0.035):
                self._auto_gain = new_gain
                self._auto_exposure_ms = new_exposure
                self._auto_pending = True

    def refresh_auto_camera_exposure(self):
        """Apply pending camera settings outside of PyCozmo's receive thread."""
        with self.lock:
            if (not self._auto_pending or self.camera_mode != "auto"
                    or self.client is None or not self.ready):
                return False
            if not self._lights_transport_ready(self.client):
                return False
            self.configure_camera()
            self._auto_pending = False
            return True

    def set_camera_auto_exposure(self):
        with self.lock:
            if self.client is None or not self.ready:
                raise RuntimeError("Cozmo disconnected")
            self.camera_mode = "auto"
            self.configure_camera()

    def set_camera_manual_exposure(self, exposure_ms):
        if type(exposure_ms) is not int or not 1 <= exposure_ms <= 67:
            raise ValueError("Manual exposure must be 1–67 ms")
        with self.lock:
            if self.client is None or not self.ready:
                raise RuntimeError("Cozmo disconnected")
            self.manual_exposure_ms = exposure_ms
            self.camera_mode = "manual"
            self.configure_camera()

    def _end_dock_assist(self, *, lower_lift):
        """Clear the complete temporary docking posture and owned headlight.

        Caller holds self.lock (RLock). Leave a manually-on headlight alone.
        """
        was_assisting = self.dock_assist
        self.dock_assist = False
        if self.docking_phase == "assisting":
            self.docking_phase = None
        if self._dock_light_owned:
            self._dock_light_owned = False
            self.light(False)
        if was_assisting and lower_lift:
            self.head(0)
            self.set_dock_lift(False)

    def start_undock(self, command_id, backoff, completed):
        """Reset docking posture, then drive forward off charging contacts."""
        with self.lock:
            if self.undock_active:
                raise ValueError("undock already in progress")
            if self.client is None or not self.ready:
                raise RuntimeError("Cozmo disconnected")
            self._end_dock_assist(lower_lift=False)
            self.stop_drive()
            # The undocking posture applies even if the contact has already
            # cleared, including a no-backoff undock request.
            self.head(0)
            self.set_dock_lift(False)
            if self.on_charger is not True:
                self.docking_phase = None
                self.on_state()
                completed(command_id, None)
                return
            self.undock_active = True
            self.docking_phase = "undocking"
            self._undock_cancel.clear()
        self.on_state()

        def worker():
            error = None
            try:
                if backoff:
                    # Cozmo's lift positioning is asynchronous. Give it a
                    # bounded moment to lower before moving off the dock.
                    started = time.monotonic()
                    deadline = started + 2.0
                    while time.monotonic() < deadline:
                        if self._undock_cancel.is_set():
                            raise RuntimeError("undock interrupted")
                        height = self.latest.get("lift_height_mm")
                        if height is not None and height <= 38:
                            break
                        # When docked Cozmo can stop reporting pose/lift.
                        # Don't hang for two seconds on absent telemetry.
                        if height is None and time.monotonic() - started >= 0.35:
                            break
                        time.sleep(0.05)
                    if self._undock_cancel.is_set():
                        raise RuntimeError("undock interrupted")
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
                    # If undocking failed and Cozmo is still on the charger,
                    # restore the parked posture rather than leaving the lift
                    # down indefinitely.
                    if error and self.on_charger is True and self.client is not None:
                        try:
                            self.set_dock_lift(True)
                        except Exception:
                            LOG.exception("Could not restore lift after failed undock")
                self.on_state()
                completed(command_id, error)

        self._undock_thread = threading.Thread(
            target=worker, name="cozmo-undock", daemon=True)
        self._undock_thread.start()

    def interrupt_undock(self):
        """A new driver command overrides a scripted undock."""
        self._undock_cancel.set()
        thread = self._undock_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
            if thread.is_alive():
                raise RuntimeError("Unable to interrupt undock operation")
        self.undock_active = False

    def assist_docking(self):
        with self.lock:
            if self.client is None or not self.ready:
                raise RuntimeError("Cozmo disconnected")
            if self.on_charger:
                return
            if self.dock_assist:
                return
            self.dock_assist = True
            self.docking_phase = "assisting"
            self.stop_drive()
            self.head(-25)
            self.set_dock_lift(True)
            if not self.headlight:
                self.light(True)
                self._dock_light_owned = True
        self.on_state()

    def cancel_docking(self):
        with self.lock:
            if self.undock_active:
                return
            if not self.dock_assist:
                return
            self.stop_drive()
            self._end_dock_assist(lower_lift=True)
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
            if not enabled:
                self._dock_light_owned = False
        self.on_state()

    def refresh_status_lights(self, mrr_connected=False):
        """Set the five steady backpack colors once per robot connection.

        The two outer LEDs only support red. The RGB center group gets
        independent blue, green and amber LEDs. No MRR-dependent changes.
        """
        with self.lock:
            if self.client is None or not self.ready:
                return False
            if self._status_light_signature == "connected":
                return False
            if not self._lights_transport_ready(self.client):
                return False
            import pycozmo
            p = pycozmo.protocol_encoder
            colors = backpack_lights()
            # PyCozmo exposes the three center and two side LEDs as
            # separate packet types, both sent once on initial connection.
            self.client.conn.send(p.LightStateCenter(states=colors[1:4]))
            self.client.conn.send(p.LightStateSide(states=(colors[0], colors[4])))
            self._status_light_signature = "connected"
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
                docked=self.battery.docked,
                docking=bool(self.dock_assist))
            if signature == self._charger_last_signature:
                return False
            if not self._lights_transport_ready(cli):
                return False
            # CubeId selects the charger, CubeLights changes that object's LEDs.
            cli.conn.send(p.CubeId(object_id=self._charger_object_id))
            cli.conn.send(p.CubeLights(states=charger_lights(
                docked=self.battery.docked,
                docking=bool(self.dock_assist))))
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
        # Do not bypass the 5-second face-send cooldown for every TTS request.
        # A rapid sequence of messages must not fill the animation keyframe
        # buffer and stall reliable wheel commands. The periodic face task
        # will display the latest message at its next permitted refresh.
        self.refresh_face()

    def refresh_face(self):
        """Send a bounded static image frame without starting its 30 Hz sender."""
        if not self._face_transport_enabled:
            return False
        return self._refresh_face_unverified()

    def _refresh_face_unverified(self):
        """Render one clipped status screen without holding the motor lock."""
        with self.lock:
            if self.client is None or not self.ready:
                return False
            now = time.monotonic()
            if now < self._face_suspended_until:
                return False
            if now - self._face_sent_at < 5.0:
                return False
            cli = self.client
            voltage = self.battery.voltage
            percent = self.battery.percent
            charging = self.battery.charging
            message = self.last_tts

        # Image/font layout and RLE compression can be expensive. NEVER hold
        # the same lock used by wheel/head/lift controls while rendering them.
        from .face import static_status_image
        from pycozmo.image_encoder import ImageEncoder, ImageDecoder
        from pycozmo.protocol_declaration import MAX_FRAME_PAYLOAD_SIZE
        # A UDP frame carries a four-byte command header plus DisplayImage's
        # two-byte array length. The old 184-byte limit was not a transport
        # limit and caused even the battery icon to be rejected.
        max_encoded_bytes = MAX_FRAME_PAYLOAD_SIZE - 6
        try:
            image = static_status_image(
                voltage=voltage, percent=percent, charging=charging,
                last_tts=message, max_encoded_bytes=max_encoded_bytes)
            encoded = bytes(ImageEncoder(image).encode())
            if not encoded or len(encoded) > max_encoded_bytes:
                raise ValueError(
                    f"Encoded face size {len(encoded)} exceeds {max_encoded_bytes} bytes")
            decoded = ImageDecoder(encoded).decode()
            foreground = sum(bool(px) for px in decoded)
            columns = sum(
                any(decoded[y * 128 + x] for y in range(32))
                for x in range(128))
            if foreground == 0 or columns < 8:
                raise ValueError(f"Encoded face unexpectedly sparse: {foreground} pixels, {columns} columns")
        except (ValueError, IndexError, AssertionError) as exc:
            with self.lock:
                self.face_last_error = str(exc)[:180]
            LOG.warning("Cozmo face skipped: %s", exc)
            return False

        with self.lock:
            # A reconnect or a newer message invalidates the rendered frame.
            if (self.client is not cli or not self.ready
                    or time.monotonic() < self._face_suspended_until
                    or self.last_tts != message
                    or time.monotonic() - self._face_sent_at < 5.0):
                return False
            sender = getattr(getattr(cli, "conn", None), "send_thread", None)
            if sender is not None:
                with sender.lock:
                    pending = ((sender.window.next_seq - sender.window.expected_seq)
                               % sender.window.max_seq)
                if pending >= 4 or sender.queue.qsize() >= 4:
                    now = time.monotonic()
                    if self._face_blocked_since is None:
                        self._face_blocked_since = now
                    elif now - self._face_blocked_since > 3:
                        # Back off temporarily rather than leaving the
                        # display permanently disabled until reconnect.
                        self._face_suspended_until = now + 10.0
                        self._face_blocked_since = None
                        self.face_last_error = (
                            "Display cooling down for 10 seconds: reliable send queue busy")
                        LOG.warning("%s", self.face_last_error)
                    return False
                self._face_blocked_since = None
            content = image.tobytes()
            if (content == self._face_last_content
                    and time.monotonic() - self._face_sent_at < 15.0):
                return False
            conn = getattr(cli, "conn", None)
            if conn is not None and callable(getattr(conn, "send", None)):
                from pycozmo.protocol_encoder import DisplayImage, OutputSilence
                # Keep the already-working bounded packet sequence untouched.
                conn.send(OutputSilence())
                conn.send(DisplayImage(image=encoded))
                conn.send(OutputSilence())
            else:
                cli.display_image(image)
            self.face_frames_sent += 1
            self._face_sent_at = time.monotonic()
            self._face_last_content = content
            self.face_last_encoded_bytes = len(encoded)
            self.face_encoded_on_pixels = foreground
            self.face_decoded_columns = columns
            self.face_last_error = None
            return True

    def advance_face_display(self):
        """Legacy compatibility: never send periodic silence packets."""
        return False

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
            self._dock_light_owned = False
            self.headlight = False
            self._dock_contact_handled = False
            self._dock_lift_target = None
            self._dock_lift_sent_at = 0.0
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
            self._stop_packet_workers()
            return
        LOG.info("Closing local PyCozmo client for reconnection")
        try:
            cli.stop()
        except Exception:
            LOG.exception("Could not close PyCozmo client")
        finally:
            self._stop_packet_workers()
