import pytest
from cozmo_roverd.protocol import hello, state, sensor, bounded_speed, ack, battery_percent

def test_hello_registers_custom_rover():
    h = hello("cozmo", "Cozmo", "#ffffff", 100)
    assert h["type"] == "hello"
    assert h["peripherals"][0]["id"] == "cozmo"
    assert h["cameraServo"]["enabled"]
    assert h["private"]["enabled"] is False
    assert h["private"]["safety"]["cliffEnabled"] is False
    assert h["private"]["safety"]["virtualWallEnabled"] is False
    assert h["private"]["safety"]["speedLimitEnabled"] is False
    assert hello("cozmo", "Cozmo", "#ffffff", 100, private=True)["private"]["enabled"] is True

def test_state_contains_required_leaves():
    s = state()["state"]
    assert set(["drivingMode", "dockingStatus", "dockAssist", "docking",
                "docked", "battery", "headlightOn", "laserOn"]) <= set(s)
    assert s["battery"]["percent"] is None

def test_speed_bounds():
    assert bounded_speed(300, 100) == 100
    assert bounded_speed(-300, 100) == -100
    with pytest.raises(ValueError):
        bounded_speed(True, 100)

def test_battery_percent_estimation():
    assert battery_percent(None) is None
    assert battery_percent(3.8) == 40
    assert state(voltage=3.8)["state"]["battery"]["percent"] == 40
    assert state(voltage=3.5)["state"]["battery"]["low"] is True


def test_sensor_does_not_fake_battery_percent():
    assert sensor(voltage=4.1)["sensors"]["voltageMv"] == 4100

def test_ack_rejection():
    assert ack({"id": "1"}, "no") == {"type": "ack", "id": "1", "status": "error", "error": "no"}


def test_docked_state_never_declares_movement_locked():
    s = state(docked=True, charging=True)["state"]
    assert s["dockingStatus"]["movementLocked"] is False
