"""PyCozmo hardware access; isolate robot callbacks from the asyncio WebSocket."""
from __future__ import annotations
import logging
import math
import threading
import time
from PIL import Image, ImageDraw
from .telemetry import snapshot
from .robot_packet import BatteryTracker, snapshot_packet
LOG = logging.getLogger(__name__)

class Robot:
    def __init__(self, *, max_speed, on_state, on_sensor, on_frame):
        self.max_speed = max_speed
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
        self.last_drive = 0.0
        self.moving = False
        self.ready = False
        self.battery = BatteryTracker()
        self.packets_received = 0
        self.last_packet_error = None
        self.packet_status = {}

    def connect(self):
        import pycozmo
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
            with self.lock:
                self.client = cli
                self.last_sensor = time.monotonic()
                self.ready = True
            self.on_state()
            LOG.info("Cozmo connected")
        except Exception:
            try: cli.disconnect()
            finally: cli.stop()
            raise

    def _charging(self, value):
        self.charging = value
        self.on_state()

    def _cliff(self, value):
        self.cliff = value
        if value: self.stop_drive()

    def _picked_up(self, value):
        self.picked_up = value
        if value: self.stop_drive()

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
            changed = old != (self.on_charger, self.charging, self.voltage,
                             self.cliff, self.picked_up)
            if changed:
                self.on_state()
        except Exception as exc:
            self.last_packet_error = str(exc)[:160]
            LOG.exception("Cozmo RobotState packet handling failed")
        # Always keep the packet receive counter available for diagnostics.

    def is_healthy(self):
        return self.ready and time.monotonic() - self.last_sensor < 5.0

    def stop_drive(self):
        with self.lock:
            if self.client is not None:
                try: self.client.drive_wheels(0, 0)
                except Exception: LOG.exception("Unable to stop wheels")
            self.moving = False

    def drive(self, left, right):
        with self.lock:
            if self.client is None: raise RuntimeError("Cozmo disconnected")
            if self.cliff or self.picked_up:
                self.stop_drive()
                raise RuntimeError("Motion blocked by cliff/pickup sensor")
            self.client.drive_wheels(left, right)
            self.moving = left != 0 or right != 0
            self.last_drive = time.monotonic()

    def watchdog(self, seconds):
        with self.lock:
            if self.moving and time.monotonic() - self.last_drive > seconds:
                LOG.warning("Drive watchdog expired; stopping")
                self.stop_drive()

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
        with self.lock:
            if self.client is None: raise RuntimeError("Cozmo disconnected")
            import pycozmo
            self.client.set_all_backpack_lights(
                pycozmo.lights.white_light if enabled else pycozmo.lights.off_light)
            self.headlight = enabled
        self.on_state()

    def face(self, text):
        image = Image.new("1", (128, 32), color=0)
        draw = ImageDraw.Draw(image)
        # Fits up to two short lines; overflow deliberately clipped.
        words = str(text).split()
        lines = ["", ""]
        for word in words:
            i = 0 if len(lines[0] + " " + word) <= 19 else 1
            if len(lines[i] + " " + word) <= 19:
                lines[i] = (lines[i] + " " + word).strip()
        for i, line in enumerate(lines):
            draw.text((2, 2 + i * 15), line, fill=1)
        with self.lock:
            if self.client is None: raise RuntimeError("Cozmo disconnected")
            self.client.display_image(image)

    def close(self):
        with self.lock:
            cli, self.client = self.client, None
            self.ready = False
            self.moving = False
            self.battery = BatteryTracker()
            self.latest = {}
            self.voltage = None
            self.on_charger = None
            self.charging = None
            self.cliff = None
            if cli is None: return
            try:
                cli.drive_wheels(0, 0)
                cli.stop_all_motors()
            except Exception: LOG.warning("Could not stop motors on disconnect", exc_info=True)
            try: cli.disconnect()
            except Exception: LOG.warning("Disconnect failed", exc_info=True)
            try: cli.stop()
            except Exception: LOG.warning("Client stop failed", exc_info=True)
