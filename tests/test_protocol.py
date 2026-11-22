import pytest
from cozmo_roverd.protocol import hello, state, sensor, bounded_speed, ack

def test_hello_registers_custom_rover():
    h = hello("cozmo", "Cozmo", "#ffffff", 100)
    assert h["type"] == "hello"
    assert h["peripherals"][0]["id"] == "cozmo"
    assert h["cameraServo"]["enabled"]
    assert h["private"] == {"enabled": False}
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

def test_sensor_does_not_fake_battery_percent():
    assert sensor(voltage=4.1)["sensors"]["voltageMv"] == 4100

def test_ack_rejection():
    assert ack({"id": "1"}, "no") == {"type": "ack", "id": "1", "status": "error", "error": "no"}
