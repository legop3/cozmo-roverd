"""Docked idle-wake behavior must not move Cozmo during active operation."""
from types import SimpleNamespace as NS

import cozmo_roverd.robot as robot_module
from cozmo_roverd.robot import Robot


class Client:
    def __init__(self):
        self.heights = []
        self.conn = NS(send_thread=None)

    def set_lift_height(self, height):
        self.heights.append(height)


def robot_at_dock(monkeypatch, start=100.0):
    now = [start]
    monkeypatch.setattr(robot_module.time, "monotonic", lambda: now[0])
    robot = Robot(max_speed=250, on_state=lambda: None,
                  on_sensor=lambda: None, on_frame=lambda image: None)
    robot.client = Client()
    robot.ready = True
    robot.on_charger = True
    robot._idle_activity_at = start
    return robot, now


def test_idle_wake_only_after_five_minutes_and_restores_lift(monkeypatch):
    robot, now = robot_at_dock(monkeypatch)
    now[0] = 399.0
    assert not robot.refresh_idle_wake()
    now[0] = 400.0
    assert robot.refresh_idle_wake()
    assert robot.client.heights == [86.0]
    now[0] = 401.0
    assert not robot.refresh_idle_wake()
    now[0] = 402.0
    assert robot.refresh_idle_wake()
    assert robot.client.heights == [86.0, 92.0]
    assert not robot.refresh_idle_wake()


def test_idle_wake_requires_dock_and_no_active_driving(monkeypatch):
    robot, now = robot_at_dock(monkeypatch)
    now[0] = 600.0
    robot.on_charger = False
    assert not robot.refresh_idle_wake()
    robot.on_charger = True
    robot.moving = True
    assert not robot.refresh_idle_wake()
    robot.moving = False
    robot.dock_assist = True
    assert not robot.refresh_idle_wake()
    robot.dock_assist = False
    robot.undock_active = True
    assert not robot.refresh_idle_wake()
    robot.undock_active = False
    assert robot.refresh_idle_wake()
    assert robot.client.heights == [86.0]


def test_driver_activity_resets_idle_timer_and_cancels_pending_restore(monkeypatch):
    robot, now = robot_at_dock(monkeypatch)
    now[0] = 400.0
    assert robot.refresh_idle_wake()
    now[0] = 401.0
    robot.note_driver_activity()
    now[0] = 402.0
    assert not robot.refresh_idle_wake()
    assert robot.client.heights == [86.0]
    now[0] = 701.0
    assert robot.refresh_idle_wake()
    assert robot.client.heights == [86.0, 86.0]


def test_no_idle_wake_when_disconnected(monkeypatch):
    robot, now = robot_at_dock(monkeypatch)
    now[0] = 500.0
    robot.ready = False
    assert not robot.refresh_idle_wake()
    assert robot.client.heights == []
