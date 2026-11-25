"""MRR control channel payloads and strictly bounded command adapter."""
from __future__ import annotations
import math
import time

def hello(name: str, description: str, color: str, max_speed: int, video: bool = True, private: bool = False) -> dict:
    return {
        "type": "hello", "name": name, "description": description,
        "color": color, "maxWheelSpeed": max_speed,
        "private": {"enabled": private, "safety": {
            "speedLimitEnabled": False,
            "hardOvercurrentEnabled": False,
            "hardBumpEnabled": False,
            "cliffEnabled": False,
            "virtualWallEnabled": False
        }},
        "cameraServo": {"enabled": True, "minAngle": -25, "maxAngle": 44,
                        "homeAngle": 10, "nudgeDegrees": 5, "allowRawPulse": False,
                        "invert": False, "minPulseUs": 1000, "maxPulseUs": 2000,
                        "pin": 0, "freqHz": 50, "cycleLen": 4096},
        "headlight": {"enabled": True, "initialOn": False},
        "laser": {"enabled": False},
        "audio": {"ttsEnabled": True, "defaultEngine": "espeak", "defaultVoice": "en-us"},
        "horn": {"enabled": True, "volume": 0.25, "sineGain": 1.0, "sawGain": 1.0,
                 "sampleRate": 22320, "channels": 1, "maxDuration": "2s"},
        "media": {"rtspPort": 8554, "video": {"enabled": video, "profile": "custom"}},
        "peripherals": [{"id": "cozmo", "name": "Cozmo", "controls": [
            {"id": "lift", "name": "Lift height (%)", "type": "slider", "min": 0, "max": 100},
            {"id": "reconnect", "name": "Reconnect Cozmo", "type": "button", "mode": "momentary"}
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


def state(*, charging=None, docked=None, voltage=None, headlight=False, driving=True, battery=None, dock_assist=False, docking_phase=None) -> dict:
    percent = battery_percent(voltage)
    if battery is None:
        battery = {"percent": percent, "charging": charging, "low": percent <= 15 if percent is not None else None, "critical": percent <= 5 if percent is not None else None, "needsCharge": percent <= 15 if percent is not None else None}
    phase = docking_phase or ("charging" if charging else "docked" if docked else "idle" if docked is False else "unknown")
    return {
        "type": "roverState", "state": {
            "drivingMode": driving, "dockAssist": dock_assist, "docking": False,
            "docked": docked,
            "dockingStatus": {"phase": phase, "movementLocked": False,
                              "cameraLocked": bool(dock_assist), "error": None},
            "battery": battery,
            "headlightOn": headlight, "laserOn": None
        }
    }

def sensor(*, voltage=None, cliff=None, left=None, right=None, on_charger=None, cliff_raw=None) -> dict:
    sensors = {}
    if voltage is not None:
        sensors["voltageMv"] = round(voltage * 1000)
    # The physical Cozmo cliff detector is a single front/underside sensor.
    # MRR's Roomba-shaped schema has no generic "any cliff" field: publish
    # its detector as one frontal cliff, never fabricate four sensors.
    if cliff is not None:
        sensors["cliffFrontLeft"] = bool(cliff)
    if cliff_raw is not None and len(cliff_raw) == 4:
        # Raw samples belong to one sensor, not four separate directions.
        # Retain the four originals in the bridge telemetry JSON.
        sensors["cliffFrontLeftSignal"] = sum(cliff_raw) / 4
    if on_charger is not None:
        sensors["chargingSources"] = {"homeBase": bool(on_charger),
                                       "internalCharger": False,
                                       "raw": 2 if on_charger else 0}
    if left is not None and right is not None:
        sensors["wheelSpeedsMmPerSecond"] = {
            "left": left, "right": right, "center": (left + right) / 2}
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
