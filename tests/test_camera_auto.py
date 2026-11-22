"""Automatic camera regulation uses sensor settings, not digital image processing."""
from types import SimpleNamespace as NS

from PIL import Image

from cozmo_roverd.robot import Robot


def make_robot():
    sent = []
    robot = Robot(
        max_speed=200, camera_mode="auto",
        on_state=lambda: None, on_sensor=lambda: None, on_frame=lambda _: None,
    )
    robot.client = NS(
        conn=NS(send=sent.append),
        camera_config=NS(
            min_gain=0.1, max_gain=3.984375,
            min_exposure_time_ms=1, max_exposure_time_ms=67,
        ),
    )
    robot.ready = True
    return robot, sent


def sample(robot, value):
    robot._auto_last_measure_at = -1000.0
    robot.observe_camera_brightness(Image.new("RGB", (320, 240),
                                              (value, value, value)))


def test_dark_room_increases_real_exposure_and_gain():
    robot, sent = make_robot()
    for _ in range(12):
        sample(robot, 15)
        assert robot.refresh_auto_camera_exposure()
    assert robot._auto_gain > 1.0
    assert robot._auto_exposure_ms > 16
    assert robot._auto_gain <= 3.984375
    assert robot._auto_exposure_ms <= 67
    assert all(not packet.auto_exposure_enabled for packet in sent)


def test_sunlight_reduces_real_exposure_then_gain():
    robot, sent = make_robot()
    robot._auto_gain = 3.984375
    robot._auto_exposure_ms = 67
    for _ in range(15):
        sample(robot, 245)
        assert robot.refresh_auto_camera_exposure()
    assert robot._auto_gain < 3.984375
    assert robot._auto_exposure_ms < 67
    assert robot._auto_gain >= 0.1
    assert robot._auto_exposure_ms >= 1
    assert all(not packet.auto_exposure_enabled for packet in sent)


def test_acceptable_brightness_does_not_enqueue_more_commands():
    robot, sent = make_robot()
    sample(robot, 105)
    assert not robot.refresh_auto_camera_exposure()
    assert sent == []


def test_automatic_sensor_changes_are_not_sent_from_camera_callback():
    robot, sent = make_robot()
    sample(robot, 245)
    assert sent == []
    assert robot._auto_pending
    assert robot.refresh_auto_camera_exposure()
    assert len(sent) == 1
    assert not robot.refresh_auto_camera_exposure()
