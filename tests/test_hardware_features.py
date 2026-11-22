"""Hardware behavior tests using small PyCozmo connection fakes."""
from types import SimpleNamespace as NS
from threading import Event
import time

from cozmo_roverd.robot import Robot


class DummyClient:
    def __init__(self):
        self.drives = []
        self.lifts = []
        self.heads = []
        self.packets = []
        self.conn = NS(send=lambda p: self.packets.append(p))
        self.camera_config = NS(
            min_gain=1.0, max_gain=8.0,
            min_exposure_time_ms=1, max_exposure_time_ms=65,
        )

    def drive_wheels(self, left, right, **kwargs):
        self.drives.append((left, right))
        self.last_acceleration = kwargs

    def move_lift(self, speed):
        self.lifts.append(speed)

    def set_head_angle(self, angle):
        self.heads.append(angle)

    def set_lift_height(self, height):
        self.lifts.append(height)


def fake_robot(mode="bright"):
    changes = []
    robot = Robot(max_speed=200, camera_mode=mode,
                  on_state=lambda: changes.append(True),
                  on_sensor=lambda: None, on_frame=lambda image: None)
    robot.client = DummyClient()
    robot.ready = True
    robot.cliff = False
    robot.picked_up = False
    return robot, changes


def test_brush_pwm_moves_lift_continuously():
    robot, _ = fake_robot()
    robot.lift_motor(127)
    robot.lift_motor(-127)
    robot.lift_motor(0)
    assert robot.client.lifts == [2.0, -2.0, 0.0]


def test_exposure_preset_sent_to_camera():
    robot, _ = fake_robot("lowlight")
    robot.configure_camera()
    pkt = robot.client.packets[-1]
    assert pkt.auto_exposure_enabled is False
    assert pkt.gain == 6.0
    assert pkt.exposure_ms == 60
    robot.camera_mode = "auto"
    robot.configure_camera()
    assert robot.client.packets[-1].auto_exposure_enabled is False


def test_undock_drives_forward_until_charger_contact_clears():
    robot, changes = fake_robot()
    robot.on_charger = True
    done = Event()
    results = []
    def complete(identifier, error):
        results.append((identifier, error))
        done.set()
    robot.start_undock("u1", True, complete)
    assert robot.client.heads == [0.0]
    assert robot.client.lifts == [32.0]
    deadline = time.monotonic() + 1
    while not robot.client.drives and time.monotonic() < deadline:
        time.sleep(0.01)
    assert robot.client.drives[0] == (200, 200)
    robot.on_charger = False
    assert done.wait(2)
    assert robot.client.drives[-1] == (0, 0)
    assert results == [("u1", None)]
    assert not robot.undock_active
    assert not robot.dock_assist
    assert changes


def test_no_orientation_based_drive_lock():
    robot, _ = fake_robot()
    robot.latest = {"orientation": "ON_BACK"}
    robot.drive(90, -90)
    assert robot.client.drives[-1] == (90, -90)


def test_cliff_alert_does_not_stop_motors_or_block_drive():
    robot, _ = fake_robot()
    robot.drive(120, 120)
    robot._cliff(True)
    assert robot.cliff is True
    assert robot.client.drives == [(120, 120)]
    robot.drive(140, -140)
    assert robot.client.drives[-1] == (140, -140)
    assert robot.moving is True


def test_commanded_speed_is_recorded_for_anki_remote_control_rate():
    robot, _ = fake_robot()
    robot.max_speed = 250
    robot.drive(250, 250)
    assert robot.client.drives[-1] == (250, 250)
    assert robot.commanded_wheels == (250, 250)


def test_charger_detection_does_not_issue_stop_during_assist():
    robot, _ = fake_robot()
    robot.dock_assist = True
    robot.drive(120, 120)
    from cozmo_roverd.robot_packet import BatteryTracker
    robot.battery = BatteryTracker()
    packet = NS(
        status=0x1000, battery_voltage=3.9, timestamp=123,
        lwheel_speed_mmps=120, rwheel_speed_mmps=120,
        head_angle_rad=0, lift_height_mm=40,
        pose_x=0, pose_y=0, pose_z=0,
        pose_angle_rad=0, pose_pitch_rad=0,
        pose_origin_id=1, pose_frame_id=1,
        accel_x=0, accel_y=0, accel_z=9.8,
        gyro_x=0, gyro_y=0, gyro_z=0,
        cliff_data_raw=(1, 1, 1, 1),
        backpack_touch_sensor_raw=0, curr_path_segment=0,
    )
    client = NS(robot_status=0x1000)
    robot._packet(client, packet)
    assert robot.client.drives[-1] == (120, 120)
    assert robot.dock_assist is False


def test_pickup_and_docked_status_never_block_direct_driving():
    robot, _ = fake_robot()
    robot.on_charger = True
    robot.picked_up = True
    robot.cliff = True
    robot.dock_assist = True
    robot.drive(250, -250)
    assert robot.client.drives[-1] == (250, -250)
    assert robot.commanded_wheels == (250, -250)
    assert robot.client.last_acceleration["lwheel_acc"] == 10000.0
    assert robot.client.last_acceleration["rwheel_acc"] == 10000.0
    robot._picked_up(True)
    robot._cliff(True)
    assert robot.client.drives[-1] == (250, -250)


def test_live_exposure_switch_uses_adaptive_sensor_control():
    from pycozmo.protocol_encoder import SetCameraParams
    robot, _ = fake_robot(mode="bright")
    robot.set_camera_auto_exposure()
    assert robot.camera_mode == "auto"
    pkt = robot.client.packets[-1]
    assert isinstance(pkt, SetCameraParams)
    assert pkt.auto_exposure_enabled is False
    assert pkt.exposure_ms == 16
    assert pkt.gain == 1.0
    robot.set_camera_manual_exposure(2)
    assert robot.camera_mode == "manual"
    pkt = robot.client.packets[-1]
    assert isinstance(pkt, SetCameraParams)
    assert pkt.auto_exposure_enabled is False
    assert pkt.exposure_ms == 2
    assert pkt.gain == 2.0
    robot.set_camera_auto_exposure()
    assert robot.client.packets[-1].auto_exposure_enabled is False
    assert robot.manual_exposure_ms == 2


def test_camera_mode_is_remembered_for_reconnect():
    robot, _ = fake_robot(mode="auto")
    robot.configure_camera()
    assert robot.client.packets[-1].auto_exposure_enabled is False
    robot.set_camera_manual_exposure(7)
    robot.client = DummyClient()
    robot.configure_camera()
    assert robot.client.packets[-1].auto_exposure_enabled is False
    assert robot.client.packets[-1].exposure_ms == 7


def test_dock_assist_raises_lift_fully():
    import math
    robot, _ = fake_robot()
    robot.on_charger = False
    robot.assist_docking()
    assert robot.dock_assist is True
    assert robot.client.lifts == [92.0]
    assert robot.client.heads == [math.radians(-25)]
    assert robot.client.drives == [(0, 0)]


def test_undock_without_backoff_levels_head_and_lowers_lift():
    robot, _ = fake_robot()
    robot.on_charger = True
    completed = Event()
    outcomes = []
    robot.start_undock("u2", False, lambda ident, err: (outcomes.append((ident, err)), completed.set()))
    assert completed.wait(2)
    assert robot.client.heads == [0.0]
    assert robot.client.lifts == [32.0]
    assert outcomes == [("u2", None)]
