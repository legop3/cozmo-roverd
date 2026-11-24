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

def snapshot(client, packet=None):
    """Called on the robot's state-event thread; no I/O, no server sends."""
    flags = int(client.robot_status)
    bits = {name: bool(flags & bit) for name, bit in FLAGS.items()}
    p = client.pose
    available = list(getattr(client, "available_objects", {}).values())[:20]
    connected = list(getattr(client, "connected_objects", {}).items())[:20]
    camera = getattr(client, "camera_config", None)
    result = {
        "timestamp_ms": int(time.time() * 1000),
        "hardware": {
            "serial_number": getattr(client, "serial_number", None),
            "head_serial_number": getattr(client, "serial_number_head", None),
            "body_hardware_version": getattr(client, "body_hw_version", None),
            "body_color": getattr(getattr(client, "body_color", None), "name", None),
        },
        "camera": {
            "last_robot_image_timestamp": getattr(client, "last_image_timestamp", None),
            "fov_x_deg": number(camera.fov_x.degrees) if camera else None,
            "fov_y_deg": number(camera.fov_y.degrees) if camera else None,
        },
        "animations": {
            "bytes_played": getattr(client, "num_anim_bytes_played", None),
            "audio_frames_played": getattr(client, "num_audio_frames_played", None),
            "enabled_tracks": getattr(client, "enabled_anim_tracks", None),
            "client_drop_count": getattr(client, "client_drop_count", None),
        },
        "cubes": {
            "available": len(getattr(client, "available_objects", {})),
            "connected": len(getattr(client, "connected_objects", {})),
            "objects": [
                {"factory_id": getattr(item, "factory_id", None),
                 "type": str(getattr(item, "object_type", "unknown"))}
                for item in available
            ],
            "connections": [
                {"object_id": int(index),
                 "factory_id": item.get("factory_id"),
                 "type": str(item.get("object_type", "unknown"))}
                for index, item in connected if isinstance(item, dict)
            ],
        },
        "battery_voltage_v": number(getattr(client, "battery_voltage", None)),
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
    if packet is not None:
        # RobotState includes information that PyCozmo's high-level Client
        # discards. Preserve it, but do not guess its physical meaning.
        result["raw_robot_state"] = {
            "robot_timestamp": getattr(packet, "timestamp", None),
            "battery_voltage_v": number(getattr(packet, "battery_voltage", None)),
            "status": int(packet.status),
            "cliff_channels": [int(x) for x in getattr(packet, "cliff_data_raw", ())],
            "backpack_touch_raw": getattr(packet, "backpack_touch_sensor_raw", None),
            "current_path_segment": getattr(packet, "curr_path_segment", None),
        }
    return result
