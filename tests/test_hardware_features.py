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

    def drive_wheels(self, left, right):
        self.drives.append((left, right))

    def move_lift(self, speed):
        self.lifts.append(speed)

    def set_head_angle(self, angle):
        self.heads.append(angle)


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
    assert robot.client.packets[-1].auto_exposure_enabled is True


def test_undock_reverses_until_charger_contact_clears():
    robot, changes = fake_robot()
    robot.on_charger = True
    done = Event()
    results = []
    def complete(identifier, error):
        results.append((identifier, error))
        done.set()
    robot.start_undock("u1", True, complete)
    deadline = time.monotonic() + 1
    while not robot.client.drives and time.monotonic() < deadline:
        time.sleep(0.01)
    assert robot.client.drives[0] == (-200, -200)
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
