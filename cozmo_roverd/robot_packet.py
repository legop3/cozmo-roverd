"""RobotState packet intake and consistent charger/battery observation.

PyCozmo's RobotState packet is the authoritative source. Keep reading packets
even if optional rich high-level telemetry has an error.
"""
from __future__ import annotations

from collections import deque
from statistics import median
import math
import time

# Based on PyCozmo's public RobotStatusFlag enum. Cozmo reports no calibrated
# percent / fuel-gauge charge, so the percentage remains an estimate.
ON_CHARGER = 0x1000
CHARGING = 0x2000
CLIFF = 0x4000
PICKED_UP = 0x8
MOVING = 0x1
FALLING = 0x20

def finite_float(value):
    try:
        v = float(value)
    except (ValueError, TypeError):
        return None
    return v if math.isfinite(v) else None

class BatteryTracker:
    def __init__(self):
        self.voltages = deque(maxlen=7)
        self.voltage = None
        self.percent = None
        self.docked = None
        self.charging = None
        self.raw_charging = None
        self.raw_docked = None
        self.low = None
        self.critical = None
        self.needs_charge = False
        self._contact_lost_since = None
        self._contact_started = None
        self._last_charging_true = None
        self._last_sample = None

    def observe(self, voltage, flags, now=None):
        from .protocol import battery_percent
        now = time.monotonic() if now is None else now
        flags = int(flags)
        self._last_sample = now
        voltage = finite_float(voltage)
        if voltage is not None and 2.7 <= voltage <= 4.4:
            self.voltages.append(voltage)
            self.voltage = voltage
            # Median over recent packets rejects electrical spikes.
            self.percent = battery_percent(median(self.voltages))
        self.raw_docked = bool(flags & ON_CHARGER)
        self.raw_charging = bool(flags & CHARGING)
        contact = self.raw_docked or self.raw_charging
        if contact:
            if self._contact_started is None:
                self._contact_started = now
            self._contact_lost_since = None
            self.docked = True
        elif self.docked:
            if self._contact_lost_since is None:
                self._contact_lost_since = now
            elif now - self._contact_lost_since >= 0.5:
                self.docked = False
                self._contact_started = None
        else:
            self.docked = False
            self._contact_started = None
        # Only IS_CHARGING indicates charging, but tolerate a brief false
        # pulse (the dock-contact flag is tracked independently).
        if self.raw_charging:
            self._last_charging_true = now
        self.charging = bool(self.raw_charging or
                             (self.docked and self._last_charging_true is not None
                              and now - self._last_charging_true < 2.0))
        if self.percent is not None:
            self.low = self.percent <= 15
            self.critical = self.percent <= 5
            if self.low:
                self.needs_charge = True
            elif self.docked and self.percent >= 30:
                # Do not release the need-to-charge latch on a single high
                # reading. Releasing it requires sustained dock contact below.
                if self._contact_started is not None and now - self._contact_started >= 120:
                    self.needs_charge = False
        return self

    def stale(self, now=None, timeout=3.0):
        now = time.monotonic() if now is None else now
        return self._last_sample is None or now - self._last_sample > timeout

    def battery_state(self):
        return {
            "percent": self.percent,
            "charging": self.charging,
            "low": self.low,
            "critical": self.critical,
            "needsCharge": self.needs_charge if self.percent is not None else None,
        }

    def diagnostic(self):
        return {
            "voltage_v": self.voltage,
            "estimated_percent": self.percent,
            "raw_docked": self.raw_docked,
            "raw_charging": self.raw_charging,
            "stable_docked": self.docked,
            "stable_charging": self.charging,
            "voltage_samples": list(self.voltages),
        }

def snapshot_packet(pkt):
    """Safely extract every scalar/array from protocol_encoder.RobotState."""
    flags = int(pkt.status)
    voltage = finite_float(pkt.battery_voltage)
    return {
        "timestamp_ms": int(time.time() * 1000),
        "battery_voltage_v": voltage if voltage is not None and 2.7 <= voltage <= 4.4 else None,
        "on_charger": bool(flags & ON_CHARGER),
        "charging": bool(flags & CHARGING),
        "cliff_detected": bool(flags & CLIFF),
        "picked_up": bool(flags & PICKED_UP),
        "moving": bool(flags & MOVING),
        "falling": bool(flags & FALLING),
        "wheel_speeds_mmps": {
            "left": finite_float(pkt.lwheel_speed_mmps),
            "right": finite_float(pkt.rwheel_speed_mmps),
            "center": finite_float((pkt.lwheel_speed_mmps + pkt.rwheel_speed_mmps)/2),
        },
        "head_angle_deg": finite_float(math.degrees(pkt.head_angle_rad)),
        "lift_height_mm": finite_float(pkt.lift_height_mm),
        "pose": {
            "x_mm": finite_float(pkt.pose_x),
            "y_mm": finite_float(pkt.pose_y),
            "z_mm": finite_float(pkt.pose_z),
            "heading_deg": finite_float(math.degrees(pkt.pose_angle_rad)),
            "pitch_deg": finite_float(math.degrees(pkt.pose_pitch_rad)),
            "origin_id": int(pkt.pose_origin_id),
            "frame_id": int(pkt.pose_frame_id),
        },
        "accelerometer": {"x": finite_float(pkt.accel_x), "y": finite_float(pkt.accel_y),
                          "z": finite_float(pkt.accel_z)},
        "gyroscope": {"x": finite_float(pkt.gyro_x), "y": finite_float(pkt.gyro_y),
                       "z": finite_float(pkt.gyro_z)},
        "status_raw": flags,
        "raw_robot_state": {
            "robot_timestamp": int(pkt.timestamp),
            "battery_voltage_v": voltage,
            "status": flags,
            "cliff_channels": [int(v) for v in pkt.cliff_data_raw],
            "backpack_touch_raw": int(pkt.backpack_touch_sensor_raw),
            "current_path_segment": int(pkt.curr_path_segment),
        },
    }
