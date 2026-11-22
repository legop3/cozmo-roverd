"""Voltage-only Li-ion charge estimator for Cozmo.

This is an approximate state-of-charge observer, not a calibrated fuel gauge:
Cozmo exposes voltage, charger flags and motor motion but no battery current.
Never infer instantaneous charge directly from a charging terminal voltage.
"""
from __future__ import annotations

import math
import time

# Conservative single-cell Li-ion approximate *resting* voltage curve.
# Calibration against a particular Cozmo battery will improve accuracy.
OCV_POINTS = (
    (3.30, 0), (3.50, 4), (3.60, 10), (3.70, 20),
    (3.75, 30), (3.80, 40), (3.85, 52), (3.90, 65),
    (3.95, 75), (4.00, 83), (4.05, 90), (4.10, 95),
    (4.17, 100),
)


def ocv_percent(voltage):
    if voltage <= OCV_POINTS[0][0]:
        return 0.0
    for (v0, p0), (v1, p1) in zip(OCV_POINTS, OCV_POINTS[1:]):
        if voltage <= v1:
            return p0 + (voltage - v0) * (p1 - p0) / (v1 - v0)
    return 100.0


def _approach(value, target, elapsed, seconds):
    return value + (target - value) * (1.0 - math.exp(-elapsed / seconds))


class VoltageFuelGauge:
    """Time-based state-of-charge observer with learned voltage sag.

    Resting samples are informative; running motors depress battery voltage.
    Charger voltage is not open-circuit voltage and must not reset charge.
    Persisted data is advisory only and revalidated on the next sample.
    """

    def __init__(self):
        self.soc = None
        self.filtered_voltage = None
        self.rest_voltage = None
        self.learned_sag_v = 0.05
        self.mode = "unknown"
        self._last_at = None
        self._rest_since = None
        self._motion_since = None
        self._pre_motion_voltage = None
        self._last_charge_seen = None
        self._charge_started = None
        self._last_full_detected = None
        self._loaded = None
        self.rest_confirmed = False
        self._rest_low_since = None
        self.low_confirmed = False

    def restore(self, data, *, now_wall=None):
        """Warm start from bounded, recent measurements; never trust old SOC."""
        now_wall = time.time() if now_wall is None else now_wall
        if not isinstance(data, dict) or data.get("version") != 1:
            return False
        try:
            updated = float(data["saved_at"])
            soc = float(data["soc"])
            sag = float(data["learned_sag_v"])
        except (KeyError, TypeError, ValueError):
            return False
        if (not all(math.isfinite(v) for v in (updated, soc, sag))
                or not 0 <= now_wall - updated <= 3600
                or not 0 <= soc <= 100 or not 0 <= sag <= 0.18):
            return False
        self._loaded = soc
        self.learned_sag_v = sag
        return True

    def export(self, *, now_wall=None):
        if self.soc is None:
            return None
        return {
            "version": 1,
            "saved_at": time.time() if now_wall is None else now_wall,
            "soc": round(self.soc, 2),
            "learned_sag_v": round(self.learned_sag_v, 4),
        }

    def observe(self, voltage, *, moving=False, charging=False, docked=False, now=None):
        now = time.monotonic() if now is None else now
        if voltage is None or not math.isfinite(voltage) or not 2.7 <= voltage <= 4.4:
            return self.soc
        elapsed = max(0.0, min(60.0, now - self._last_at)) if self._last_at is not None else 0.0
        self._last_at = now

        # Basic analog noise filtering, independent of packet frequency.
        if self.filtered_voltage is None:
            self.filtered_voltage = voltage
        else:
            self.filtered_voltage = _approach(
                self.filtered_voltage, voltage, elapsed, 8.0)
        v = self.filtered_voltage
        mode = "charging" if charging else "docked" if docked else "moving" if moving else "rest"
        prior = self.mode
        self.mode = mode
        if self.soc is None:
            # Charging voltage is biased high; warm-start saved SOC when present.
            # A first-ever estimate is explicitly uncertain.
            # Under wheel load the terminal voltage can be far below its
            # open-circuit value. Avoid initializing at an artificially low SOC.
            first = ocv_percent(min(4.17, v + (0.12 if mode == "moving" else 0)))
            if self._loaded is not None:
                first = self._loaded
                if mode == "rest" and abs(first - ocv_percent(v)) > 35:
                    first = ocv_percent(v)
            self.soc = first
            self._loaded = None
            self.rest_voltage = v if mode == "rest" else None

        if mode == "rest":
            if prior != "rest":
                self._rest_since = now
                self._motion_since = None
                self._pre_motion_voltage = None
            if self._rest_since is None:
                self._rest_since = now
            # Let voltage recover after motors stop before trusting it as OCV.
            if now - self._rest_since >= 12.0:
                self.rest_voltage = v if self.rest_voltage is None else _approach(
                    self.rest_voltage, v, elapsed, 30.0)
                target = ocv_percent(self.rest_voltage)
                self.rest_confirmed = True
                # Slow down recalibration; allow real SOC correction over time.
                self.soc = _approach(self.soc, target, elapsed, 100.0)
                # Low-battery status requires sustained resting evidence, not
                # a momentary motor-induced drop or a charging-voltage guess.
                if target <= 15:
                    if self._rest_low_since is None:
                        self._rest_low_since = now
                    if now - self._rest_low_since >= 25.0:
                        self.low_confirmed = True
                elif target >= 25:
                    self._rest_low_since = None
                    self.low_confirmed = False
        elif mode == "moving":
            if prior != "moving":
                self._motion_since = now
                # Capture the rested baseline for optional sag learning.
                self._pre_motion_voltage = (self.rest_voltage
                                             if prior == "rest" else None)
            if (self._pre_motion_voltage is not None and self._motion_since is not None
                    and 2.0 <= now - self._motion_since <= 8.0):
                # Motion voltage drop estimates load sag; clamp out non-motor
                # disturbances and learn conservatively.
                sag = max(0.0, min(0.18, self._pre_motion_voltage - v))
                self.learned_sag_v = _approach(
                    self.learned_sag_v, sag, elapsed, 35.0)
            # During load, charge may fall but cannot rise from a sag estimate.
            target = ocv_percent(min(4.17, v + self.learned_sag_v))
            if target < self.soc:
                self.soc = max(
                    self.soc - 0.07 * elapsed,
                    _approach(self.soc, target, elapsed, 180.0))
            self._rest_since = None
        else:
            self._rest_since = None
            self._motion_since = None
            self._pre_motion_voltage = None
            if charging:
                if self._charge_started is None:
                    self._charge_started = now
                self._last_charge_seen = now
                # Terminal voltage on a charger overstates SOC. Use it only as
                # an *upper bound*, with a very conservative time limit.
                upper = ocv_percent(min(4.17, v))
                if upper > self.soc:
                    self.soc = min(upper, self.soc + elapsed / 120.0)
            else:
                # Dock contact alone is not evidence of charging.
                # Keep the estimate stable rather than jumping with voltage.
                if (self._last_charge_seen is not None
                        and self._charge_started is not None
                        and self._last_charge_seen - self._charge_started >= 120
                        and now - self._last_charge_seen >= 120
                        and v >= 4.12 and self._last_full_detected != self._charge_started):
                    # Cozmo ceased charging after a sustained charging period,
                    # while still at near-full terminal voltage.
                    self.soc = max(self.soc, 98.0)
                    self._last_full_detected = self._charge_started

        if not docked and not charging:
            self._charge_started = None
            self._last_charge_seen = None
        self.soc = max(0.0, min(100.0, self.soc))
        return self.soc

    def diagnostics(self):
        return {
            "mode": self.mode,
            "estimated_percent_float": round(self.soc, 2) if self.soc is not None else None,
            "filtered_voltage_v": round(self.filtered_voltage, 4) if self.filtered_voltage is not None else None,
            "rest_voltage_v": round(self.rest_voltage, 4) if self.rest_voltage is not None else None,
            "learned_motor_sag_v": round(self.learned_sag_v, 4),
            "method": "voltage_only_state_estimator",
            "rest_confirmed": self.rest_confirmed,
            "low_confirmed": self.low_confirmed,
        }
