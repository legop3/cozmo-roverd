"""Read PyCozmo's real state without pretending it is Roomba telemetry.

MRR has a fixed sensor schema. Detailed Cozmo-only metrics live in this
snapshot and are also available from the bridge's own status web UI.
"""
from __future__ import annotations

import math
import time

FLAGS = {
    "moving": 0x1,
    "carrying_block": 0x2,
    "picking_or_placing": 0x4,
    "picked_up": 0x8,
    "body_acc_mode": 0x10,
    "falling": 0x20,
    "animating": 0x40,
    "pathing": 0x80,
    "lift_in_position": 0x100,
    "head_in_position": 0x200,
    "animation_buffer_full": 0x400,
    "animating_idle": 0x800,
    "on_charger": 0x1000,
    "charging": 0x2000,
    "cliff_detected": 0x4000,
    "wheels_moving": 0x8000,
    "charger_out_of_service": 0x10000,
}

def number(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return round(n, 3) if math.isfinite(n) else None

def vector(value):
    return {axis: number(getattr(value, axis, None)) for axis in ("x", "y", "z")}

def snapshot(client):
    """Called on the robot's state-event thread; no I/O, no server sends."""
    flags = int(client.robot_status)
    bits = {name: bool(flags & bit) for name, bit in FLAGS.items()}
    p = client.pose
    return {
        "timestamp_ms": int(time.time() * 1000),
        "battery_voltage_v": number(client.battery_voltage) if client.battery_voltage > 0 else None,
        "on_charger": bits["on_charger"],
        "charging": bits["charging"],
        "cliff_detected": bits["cliff_detected"],
        "picked_up": bits["picked_up"],
        "moving": bits["moving"],
        "wheel_speeds_mmps": {
            "left": number(client.left_wheel_speed.mmps),
            "right": number(client.right_wheel_speed.mmps),
            "center": number((client.left_wheel_speed.mmps + client.right_wheel_speed.mmps) / 2),
        },
        "head_angle_deg": number(client.head_angle.degrees),
        "lift_height_mm": number(client.lift_position.height.mm),
        "pose": {
            "x_mm": number(p.position.x),
            "y_mm": number(p.position.y),
            "z_mm": number(p.position.z),
            "heading_deg": number(p.rotation.angle_z.degrees),
            "pitch_deg": number(client.pose_pitch.degrees),
            "origin_id": p.origin_id,
            "frame_id": client.pose_frame_id,
        },
        "accelerometer": vector(client.accel),
        "gyroscope": vector(client.gyro),
        "orientation": getattr(client.robot_orientation, "name", str(client.robot_orientation)),
        "flags": bits,
        "status_raw": flags,
    }
