"""RobotState packet intake and consistent charger/battery observation.

PyCozmo's RobotState packet is the authoritative source. Keep reading packets
even if optional rich high-level telemetry has an error.
"""
from __future__ import annotations

from collections import deque
from statistics import median
import math
import time
from .battery_gauge import VoltageFuelGauge

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
        self._smoothed_voltage = None
        self._smoothed_at = None
        self.gauge = VoltageFuelGauge()
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

    def observe(self, voltage, flags, now=None, moving=False):
        now = time.monotonic() if now is None else now
        flags = int(flags)
        self._last_sample = now
        voltage = finite_float(voltage)
        valid_voltage = voltage is not None and 2.7 <= voltage <= 4.4
        if valid_voltage:
            self.voltages.append(voltage)
            self.voltage = voltage
        # Interpret charging and motion before estimating charge. Never use
        # charger terminal voltage as an instantaneous percentage reading.
        charge_now = bool(flags & CHARGING)
        dock_now = bool(flags & ON_CHARGER) or charge_now
        self.gauge.observe(
            median(self.voltages) if valid_voltage else None,
            moving=bool(moving or flags & MOVING),
            charging=charge_now, docked=dock_now, now=now)
        self._smoothed_voltage = self.gauge.filtered_voltage
        if self.gauge.soc is not None:
            # Publish only integer changes; the float estimate is filtered
            # continuously by the model and exposed separately in diagnostics.
            self.percent = round(self.gauge.soc)
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
            # SOC inferred while charging or moving is too uncertain to
            # trigger MRR's low/critical indicators. Low requires a sustained
            # low-voltage rest period; charging clears the active warning.
            self.low = bool(self.gauge.low_confirmed and not self.docked)
            self.critical = bool(self.gauge.critical_confirmed and not self.docked)
            if self.low:
                self.needs_charge = True
            elif self.docked and self.percent >= 30:
                if (self._contact_started is not None
                        and now - self._contact_started >= 120):
                    self.needs_charge = False
            elif not self.docked and self.percent >= 25:
                self.needs_charge = False
        return self

    def age_seconds(self, now=None):
        if self._last_sample is None:
            return None
        now = time.monotonic() if now is None else now
        return round(max(0.0, now - self._last_sample), 2)

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
            "smoothed_voltage_v": self._smoothed_voltage,
            "fuel_gauge": self.gauge.diagnostics(),
            "raw_docked": self.raw_docked,
            "raw_charging": self.raw_charging,
            "stable_docked": self.docked,
            "stable_charging": self.charging,
            "voltage_samples": list(self.voltages),
            "age_seconds": self.age_seconds(),
            "stale": self.stale(),
        }

def lift_height_from_robot_state(value):
    """Convert firmware lift-angle readings to actual lift height in mm.

    Although pycozmo names RobotState's field `lift_height_mm`, Cozmo
    firmware 2381 reports approximately -0.20 to +0.79 here: these are
    radians of lift-arm rotation, *not* millimeters. Preserve compatibility
    with actual millimeter samples if ever received. Geometry matches
    pycozmo.robot: pivot 45 mm, arm length 66 mm.
    """
    raw = finite_float(value)
    if raw is None:
        return None
    if -0.4 <= raw <= 1.2:
        return max(32.0, min(92.0, 45.0 + 66.0 * math.sin(raw)))
    if 20.0 <= raw <= 110.0:
        return raw
    return None


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
        "lift_height_mm": lift_height_from_robot_state(pkt.lift_height_mm),
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
            "lift_raw": finite_float(pkt.lift_height_mm),
            "battery_voltage_v": voltage,
            "status": flags,
            "cliff_channels": [int(v) for v in pkt.cliff_data_raw],
            "backpack_touch_raw": int(pkt.backpack_touch_sensor_raw),
            "current_path_segment": int(pkt.curr_path_segment),
        },
    }
