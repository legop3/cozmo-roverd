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

# Cozmo has no battery-current/fuel-gauge telemetry. Charging recovery is
# based on confirmed charger flags and elapsed *observed* charging time,
# never the percentage inferred from biased charger-terminal voltage.
RECOVERY_WITH_VOLTAGE_SECONDS = 15 * 60
RECOVERY_NO_VOLTAGE_SECONDS = 35 * 60
RECOVERY_MIN_VOLTAGE = 3.65
RECOVERY_MIN_SAFE_VOLTAGE = 3.45
RECOVERY_SAMPLE_GAP_SECONDS = 10
RECOVERY_LOST_CONTACT_SECONDS = 30
RECOVERY_VOLTAGE_MAX_AGE_SECONDS = 60

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
        self._needs_charge_known = False
        self._contact_lost_since = None
        self._contact_started = None
        self._last_charging_true = None
        self._last_sample = None
        self._voltage_at_wall = None
        self._charge_recovery_seconds = 0.0
        self._charge_recovery_last_at = None
        self._charge_recovery_last_contact_at = None
        self._charge_recovery_last_charging = False
        self._charge_recovery_voltage_at = None
        self._charge_recovery_voltage = None

    def voltage_snapshot(self):
        if self.voltage is None or self._voltage_at_wall is None:
            return {}
        return {"last_battery_voltage_v": self.voltage,
                "last_battery_voltage_at": self._voltage_at_wall}

    def charge_state_snapshot(self):
        """Persist a low-battery recovery obligation across daemon restarts."""
        return {"needs_charge": self.needs_charge,
                "needs_charge_known": self._needs_charge_known}

    def restore_charge_state(self, data):
        """Restore only recent, explicit charge obligations; not elapsed credit."""
        if not isinstance(data, dict) or data.get("version") != 1:
            return False
        saved_at = finite_float(data.get("saved_at"))
        if (saved_at is None or not 0 <= time.time() - saved_at <= 3600
                or type(data.get("needs_charge")) is not bool
                or type(data.get("needs_charge_known")) is not bool):
            return False
        self.needs_charge = data["needs_charge"]
        self._needs_charge_known = data["needs_charge_known"]
        return True

    def restore_voltage(self, data):
        """Restore a recent *measured* voltage, never synthesize a new one."""
        if not isinstance(data, dict):
            return False
        voltage = finite_float(data.get("last_battery_voltage_v"))
        saved_at = finite_float(data.get("last_battery_voltage_at"))
        if (voltage is None or not 2.7 <= voltage <= 4.4 or saved_at is None
                or not 0 <= time.time() - saved_at <= 3600):
            return False
        if self.voltage is None:
            self.voltage = voltage
            self._voltage_at_wall = saved_at
            return True
        return False

    def voltage_age_seconds(self, now_wall=None):
        if self._voltage_at_wall is None:
            return None
        now_wall = time.time() if now_wall is None else now_wall
        return round(max(0.0, now_wall - self._voltage_at_wall), 2)

    def observe(self, voltage, flags, now=None, moving=False):
        now = time.monotonic() if now is None else now
        flags = int(flags)
        self._last_sample = now
        voltage = finite_float(voltage)
        valid_voltage = voltage is not None and 2.7 <= voltage <= 4.4
        if valid_voltage:
            self._charge_recovery_voltage = voltage
            self._charge_recovery_voltage_at = now
            self.voltages.append(voltage)
            self.voltage = voltage
            self._voltage_at_wall = time.time()
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
        # Only count *observed charging* with fresh RobotState packets.
        # A missing packet, stale dock contact, or charging pause never earns
        # recovery time. Brief on-dock pauses retain already-earned time.
        if contact:
            self._charge_recovery_last_contact_at = now
        elif (self._charge_recovery_last_contact_at is None
              or now - self._charge_recovery_last_contact_at
                 >= RECOVERY_LOST_CONTACT_SECONDS):
            self._charge_recovery_seconds = 0.0
        prior_at = self._charge_recovery_last_at
        gap = now - prior_at if prior_at is not None else None
        if gap is not None and gap > RECOVERY_LOST_CONTACT_SECONDS:
            # Do not carry credit across a lost connection / stale telemetry.
            self._charge_recovery_seconds = 0.0
        if (contact and charge_now and self._charge_recovery_last_charging
                and gap is not None and 0 < gap <= RECOVERY_SAMPLE_GAP_SECONDS):
            self._charge_recovery_seconds += gap
        self._charge_recovery_last_at = now
        self._charge_recovery_last_charging = contact and charge_now

        # A persisted charge obligation can be cleared by observed charging
        # even if Cozmo reports zero volts while docked and SOC stays unknown.
        if self.percent is not None:
            self._needs_charge_known = True
            # Charger-terminal voltage is not reliable resting SOC.
            self.low = bool(self.gauge.low_confirmed and not self.docked)
            self.critical = bool(self.gauge.critical_confirmed and not self.docked)
            if self.low:
                self.needs_charge = True
        if self.needs_charge and self.docked:
            voltage_age = (now - self._charge_recovery_voltage_at
                           if self._charge_recovery_voltage_at is not None
                           else None)
            fresh_voltage = (voltage_age is not None
                             and 0 <= voltage_age <= RECOVERY_VOLTAGE_MAX_AGE_SECONDS)
            last_voltage = self._charge_recovery_voltage
            # Recent severely low voltage is stronger evidence than time on
            # the charger. No-voltage fallback requires longer actual charging.
            safe_voltage = (not fresh_voltage or last_voltage >= RECOVERY_MIN_SAFE_VOLTAGE)
            verified_voltage = fresh_voltage and last_voltage >= RECOVERY_MIN_VOLTAGE
            charge_time = self._charge_recovery_seconds
            if safe_voltage and (
                (verified_voltage and charge_time >= RECOVERY_WITH_VOLTAGE_SECONDS)
                or (not fresh_voltage and charge_time >= RECOVERY_NO_VOLTAGE_SECONDS)
                or (verified_voltage and self.gauge.soc is not None
                    and self.gauge.soc >= 98 and charge_time >= 120)
            ):
                self.needs_charge = False
                self._needs_charge_known = True
                # Do not carry the old low-voltage latch into the next drive.
                self.gauge.low_confirmed = False
                self.gauge.critical_confirmed = False
                self.gauge._rest_low_since = None
                self.low = False
                self.critical = False
                self._charge_recovery_seconds = 0.0
        elif self.needs_charge and not self.docked and self.percent is not None:
            # Only fresh, sustained rested voltage can clear recovery off-dock.
            if (self.gauge.rest_confirmed
                    and self.gauge.rest_voltage is not None
                    and self.gauge.rest_voltage >= 3.60
                    and not self.gauge.low_confirmed):
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
            "needsCharge": self.needs_charge if self._needs_charge_known else None,
        }

    def diagnostic(self):
        return {
            "voltage_v": self.voltage,
            "voltage_age_seconds": self.voltage_age_seconds(),
            "voltage_sample_at": self._voltage_at_wall,
            "estimated_percent": self.percent,
            "smoothed_voltage_v": self._smoothed_voltage,
            "fuel_gauge": self.gauge.diagnostics(),
            "raw_docked": self.raw_docked,
            "raw_charging": self.raw_charging,
            "stable_docked": self.docked,
            "stable_charging": self.charging,
            "needs_charge_known": self._needs_charge_known,
            "recovery_charge_seconds": round(self._charge_recovery_seconds, 1),
            "recovery_with_voltage_seconds": RECOVERY_WITH_VOLTAGE_SECONDS,
            "recovery_no_voltage_seconds": RECOVERY_NO_VOLTAGE_SECONDS,
            "recovery_voltage_age_seconds": (
                round(time.monotonic() - self._charge_recovery_voltage_at, 1)
                if self._charge_recovery_voltage_at is not None else None),
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
