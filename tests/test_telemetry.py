"""PyCozmo fields are sourced from upstream RobotState and status flags."""
from types import SimpleNamespace as NS
from cozmo_roverd.telemetry import snapshot
from cozmo_roverd.robot import Robot
from cozmo_roverd.protocol import state


def fake_client(flags=0x1000, battery=3.87):
    angle = lambda degrees: NS(degrees=degrees)
    return NS(
        robot_status=flags, battery_voltage=battery,
        pose=NS(position=NS(x=120.0, y=-30.0, z=3.0),
                rotation=NS(angle_z=angle(37.5)), origin_id=7),
        pose_frame_id=12,
        pose_pitch=angle(2.0), head_angle=angle(15.0),
        lift_position=NS(height=NS(mm=70.0)),
        left_wheel_speed=NS(mmps=50.0), right_wheel_speed=NS(mmps=-20.0),
        accel=NS(x=1.0, y=2.0, z=3.0),
        gyro=NS(x=0.1, y=-0.2, z=0.3),
        robot_orientation=NS(name="ON_THREADS"),
    )


def test_telemetry_snapshot_maps_real_pyc_status_flags():
    item = snapshot(fake_client(0x1000 | 0x2000 | 0x8 | 0x4000))
    assert item["on_charger"] is True
    assert item["charging"] is True
    assert item["cliff_detected"] is True
    assert item["picked_up"] is True
    assert item["battery_voltage_v"] == 3.87
    assert item["pose"]["heading_deg"] == 37.5
    assert item["head_angle_deg"] == 15
    assert item["lift_height_mm"] == 70
    assert item["wheel_speeds_mmps"]["center"] == 15
    assert item["accelerometer"]["y"] == 2


def test_charger_contact_distinct_from_actively_charging():
    item = snapshot(fake_client(0x1000))
    assert item["on_charger"] is True
    assert item["charging"] is False
    output = state(docked=item["on_charger"], charging=item["charging"],
                   voltage=item["battery_voltage_v"])["state"]
    assert output["docked"] is True
    assert output["battery"]["charging"] is False
    assert output["dockingStatus"]["phase"] == "docked"


def fake_packet(flags=0x1000, battery=3.87):
    return NS(status=flags, battery_voltage=battery, timestamp=42,
              lwheel_speed_mmps=50.0, rwheel_speed_mmps=-20.0,
              head_angle_rad=0.25, lift_height_mm=70.0,
              pose_x=120.0, pose_y=-30.0, pose_z=3.0,
              pose_angle_rad=0.5, pose_pitch_rad=0.1,
              pose_origin_id=7, pose_frame_id=12,
              accel_x=1.0, accel_y=2.0, accel_z=3.0,
              gyro_x=0.1, gyro_y=-0.2, gyro_z=0.3,
              cliff_data_raw=(120, 150, 170, 200),
              backpack_touch_sensor_raw=220, curr_path_segment=4)


def test_robot_packet_updates_battery_and_charger():
    events = []
    robot = Robot(max_speed=100, on_state=lambda: events.append("state"),
                  on_sensor=lambda: events.append("sensor"),
                  on_frame=lambda frame: None)
    robot._packet(fake_client(flags=0x3000, battery=3.9), fake_packet(0x3000, 3.9))
    assert robot.on_charger is True
    assert robot.charging is True
    assert robot.voltage == 3.9
    assert robot.latest["status_raw"] == 0x3000
    assert robot.latest["raw_robot_state"]["backpack_touch_raw"] == 220
    assert robot.packets_received == 1
    robot._packet(fake_client(flags=0, battery=3.9), fake_packet(0, 3.9))
    assert robot.packets_received == 2
    assert robot.on_charger is True
    assert events


def test_battery_tracker_rejects_spikes_and_stale_state():
    from cozmo_roverd.robot_packet import BatteryTracker
    tracker = BatteryTracker()
    for v in (3.82, 3.81, 3.83, 4.40, 3.82):
        tracker.observe(v, 0, now=100)
    assert tracker.percent is not None
    assert tracker.percent < 70
    assert not tracker.stale(now=101)
    assert tracker.stale(now=104)


def test_bridge_state_sender_resends_snapshot_and_never_needs_sensor_queue():
    import asyncio
    import json
    from cozmo_roverd.main import Bridge
    class Socket:
        def __init__(self):
            self.messages = []
        async def send(self, value):
            self.messages.append(json.loads(value))
    async def exercise():
        bridge = object.__new__(Bridge)
        bridge.hardware_ready = asyncio.Event()
        bridge.hardware_ready.set()
        bridge.stop_event = asyncio.Event()
        bridge.state_dirty = asyncio.Event()
        bridge.results = asyncio.Queue()
        from cozmo_roverd.robot_packet import BatteryTracker
        tracker = BatteryTracker()
        tracker.observe(3.9, 0x1000)
        bridge.robot = NS(
            charging=tracker.charging, on_charger=True, headlight=False,
            voltage=3.9, ready=True, battery=tracker,
            dock_assist=False, docking_phase=None,
            latest={"battery_voltage_v": 3.9, "on_charger": True,
                    "wheel_speeds_mmps": {"left": 8, "right": 9},
                    "cliff_detected": True,
                    "raw_robot_state": {"cliff_channels": [120, 130, 140, 150]}},
        )
        socket = Socket()
        task = asyncio.create_task(bridge._send(socket))
        await asyncio.sleep(0.3)
        bridge.hardware_ready.clear()
        await task
        return socket.messages
    messages = asyncio.run(exercise())
    state_msgs = [m for m in messages if m["type"] == "roverState"]
    sensor_msgs = [m for m in messages if m["type"] == "sensor"]
    assert state_msgs
    assert state_msgs[0]["state"]["docked"] is True
    assert state_msgs[0]["state"]["battery"]["percent"] is not None
    assert state_msgs[0]["state"]["battery"]["charging"] is False
    assert sensor_msgs
    assert sensor_msgs[0]["sensors"]["voltageMv"] == 3900
    assert sensor_msgs[0]["sensors"]["cliffFrontLeft"] is True
    assert sensor_msgs[0]["sensors"]["cliffFrontLeftSignal"] == 135
