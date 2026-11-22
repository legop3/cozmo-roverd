"""MRR control channel payloads and strictly bounded command adapter."""
from __future__ import annotations
import math
import time

def hello(name: str, description: str, color: str, max_speed: int, video: bool = True, private: bool = False) -> dict:
    return {
        "type": "hello", "name": name, "description": description,
        "color": color, "maxWheelSpeed": max_speed,
        "private": {"enabled": private},
        "cameraServo": {"enabled": True, "minAngle": -25, "maxAngle": 44,
                        "homeAngle": 10, "nudgeDegrees": 5, "allowRawPulse": False,
                        "invert": False, "minPulseUs": 1000, "maxPulseUs": 2000,
                        "pin": 0, "freqHz": 50, "cycleLen": 4096},
        "headlight": {"enabled": True, "initialOn": False},
        "laser": {"enabled": False},
        "audio": {"ttsEnabled": False},
        "horn": {"enabled": False},
        "media": {"rtspPort": 8554, "video": {"enabled": video, "profile": "custom"}},
        "peripherals": [{"id": "cozmo", "name": "Cozmo", "controls": [
            {"id": "lift", "name": "Lift height (%)", "type": "slider", "min": 0, "max": 100},
            {"id": "face", "name": "Face text", "type": "text", "maxLength": 80},
            {"id": "light", "name": "Backpack lights", "type": "button", "mode": "toggle"},
            {"id": "volume", "name": "Speaker volume", "type": "slider", "min": 0, "max": 100},
            {"id": "clear_face", "name": "Clear face display", "type": "button", "mode": "momentary"}
        ]}],
    }

def battery_percent(voltage):
    """Approximate single-cell LiPo charge; voltage is not a fuel gauge."""
    if voltage is None or not math.isfinite(voltage) or voltage < 2.5 or voltage > 4.5:
        return None
    points = [(3.3, 0), (3.55, 5), (3.7, 20), (3.8, 40),
              (3.9, 60), (4.0, 80), (4.1, 95), (4.2, 100)]
    if voltage <= points[0][0]:
        return 0
    for (lo, a), (hi, b) in zip(points, points[1:]):
        if voltage <= hi:
            return round(a + (voltage - lo) * (b - a) / (hi - lo))
    return 100


def state(*, charging=None, docked=None, voltage=None, headlight=False, driving=True, battery=None) -> dict:
    percent = battery_percent(voltage)
    if battery is None:
        battery = {"percent": percent, "charging": charging, "low": percent <= 15 if percent is not None else None, "critical": percent <= 5 if percent is not None else None, "needsCharge": percent <= 15 if percent is not None else None}
    phase = "charging" if charging else "docked" if docked else "idle" if docked is False else "unknown"
    return {
        "type": "roverState", "state": {
            "drivingMode": driving, "dockAssist": None, "docking": None,
            "docked": docked,
            "dockingStatus": {"phase": phase, "movementLocked": False,
                              "cameraLocked": False, "error": None},
            "battery": battery,
            "headlightOn": headlight, "laserOn": None
        }
    }

def sensor(*, voltage=None, cliff=None, left=None, right=None, on_charger=None) -> dict:
    sensors = {}
    if voltage is not None:
        sensors["voltageMv"] = int(voltage * 1000)
    # Cozmo's global cliff flag is not specifically its left sensor.
    # Do not manufacture directional cliff readings.
    if on_charger is not None:
        sensors["chargingSources"] = {"homeBase": bool(on_charger), "internalCharger": False, "raw": 2 if on_charger else 0}
    if left is not None and right is not None:
        sensors["wheelSpeedsMmPerSecond"] = {"left": left, "right": right, "center": (left + right) / 2}
    return {"type": "sensor", "ts": int(time.time() * 1000), "sensors": sensors}

def bounded_speed(v, maximum):
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError("wheel speed must be an integer")
    return max(-maximum, min(maximum, v))

def finite(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError("expected a finite number")
    return float(v)

def ack(command: dict, error: str | None = None) -> dict:
    value = {"type": "ack", "id": command["id"], "status": "error" if error else "ok"}
    if error: value["error"] = error[:180]
    return value
