"""MRR control channel payloads and strictly bounded command adapter."""
from __future__ import annotations
import math
import time

def hello(name: str, description: str, color: str, max_speed: int, video: bool = True) -> dict:
    return {
        "type": "hello", "name": name, "description": description,
        "color": color, "maxWheelSpeed": max_speed,
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
            {"id": "light", "name": "Backpack lights", "type": "button", "mode": "toggle"}
        ]}],
    }

def state(*, charging=None, docked=None, voltage=None, headlight=False) -> dict:
    return {
        "type": "roverState", "state": {
            "drivingMode": True, "dockAssist": None, "docking": None,
            "docked": docked,
            "dockingStatus": {"phase": "unknown", "movementLocked": False,
                              "cameraLocked": False, "error": None},
            "battery": {"percent": None, "charging": charging,
                        "low": None, "critical": None, "needsCharge": None},
            "headlightOn": headlight, "laserOn": None
        }
    }

def sensor(*, voltage=None, cliff=None, left=None, right=None) -> dict:
    sensors = {}
    if voltage is not None:
        sensors["voltageMv"] = int(voltage * 1000)
    if cliff is not None:
        sensors["cliffLeft"] = bool(cliff)
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
