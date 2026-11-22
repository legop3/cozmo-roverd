"""PyCozmo hardware access; isolate robot callbacks from the asyncio WebSocket."""
from __future__ import annotations
import logging
import math
import threading
import time
from PIL import Image, ImageDraw, ImageFont
LOG = logging.getLogger(__name__)

class Robot:
    def __init__(self, *, max_speed, on_state, on_sensor, on_frame):
        self.max_speed = max_speed
        self.on_state, self.on_sensor, self.on_frame = on_state, on_sensor, on_frame
        self.client = None
        self.lock = threading.RLock()
        self.charging = None
        self.cliff = None
        self.picked_up = False
        self.headlight = False
        self.voltage = None
        self.last_sensor = 0.0
        self.last_drive = 0.0
        self.moving = False
        self.ready = False
        self._last_battery_state = None

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
            cli.add_handler(pycozmo.event.EvtRobotStateUpdated,
                            lambda _client: self._sample(cli))
            cli.enable_camera(enable=True, color=False)
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
        self.on_sensor()

    def _picked_up(self, value):
        self.picked_up = value
        if value: self.stop_drive()
        self.on_sensor()

    def _sample(self, cli):
        from .protocol import battery_percent
        self.voltage = cli.battery_voltage if cli.battery_voltage > 0 else None
        self.last_sensor = time.monotonic()
        self.on_sensor()
        percent = battery_percent(self.voltage)
        # Emit battery state changes when the displayed percentage changes;
        # raw voltage continues to stream independently as sensor telemetry.
        if percent != self._last_battery_state:
            self._last_battery_state = percent
            self.on_state()

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
            if cli is None: return
            try:
                cli.drive_wheels(0, 0)
                cli.stop_all_motors()
            except Exception: LOG.warning("Could not stop motors on disconnect", exc_info=True)
            try: cli.disconnect()
            except Exception: LOG.warning("Disconnect failed", exc_info=True)
            try: cli.stop()
            except Exception: LOG.warning("Client stop failed", exc_info=True)
