"""Keep actual Cozmo battery telemetry separate from percentage estimation."""
import math
import time

from cozmo_roverd.robot_packet import BatteryTracker, CHARGING, ON_CHARGER, snapshot_packet


class FakeState:
    """Minimal raw RobotState packet used by the packet-snapshot decoder."""
    battery_voltage = 4.699951171875
    status = CHARGING | ON_CHARGER
    lwheel_speed_mmps = 0
    rwheel_speed_mmps = 0
    head_angle_rad = 0
    lift_height_mm = 0
    pose_x = 0
    pose_y = 0
    pose_z = 0
    pose_angle_rad = 0
    pose_pitch_rad = 0
    pose_origin_id = 0
    pose_frame_id = 0
    accel_x = 0
    accel_y = 0
    accel_z = 0
    gyro_x = 0
    gyro_y = 0
    gyro_z = 0
    timestamp = 42
    cliff_data_raw = ()
    backpack_touch_sensor_raw = 0
    curr_path_segment = 0


def test_docked_charging_voltage_is_preserved_in_all_reported_fields():
    battery = BatteryTracker()
    battery.observe(3.96795654296875, 0)
    age_before = battery.voltage_age_seconds()
    battery.observe(4.699951171875, CHARGING | ON_CHARGER)
    assert battery.voltage == 4.699951171875
    assert battery.diagnostic()["voltage_v"] == 4.699951171875
    assert battery.voltage_age_seconds() <= max(1.0, age_before)
    assert battery.docked is True
    assert battery.charging is True
    assert snapshot_packet(FakeState())["battery_voltage_v"] == 4.699951171875
    assert snapshot_packet(FakeState())["raw_robot_state"]["battery_voltage_v"] == 4.699951171875


def test_voltage_persistence_does_not_discard_charger_reading():
    original = BatteryTracker()
    original.observe(4.699951171875, CHARGING | ON_CHARGER)
    saved = original.voltage_snapshot()
    restored = BatteryTracker()
    assert restored.restore_voltage(saved)
    assert restored.voltage == 4.699951171875


def test_nonfinite_voltage_is_not_published_or_persisted():
    battery = BatteryTracker()
    battery.observe(4.699951171875, CHARGING | ON_CHARGER)
    battery.observe(float("nan"), CHARGING | ON_CHARGER)
    assert battery.voltage == 4.699951171875
    assert math.isfinite(battery.voltage)
    assert battery.voltage_snapshot()["last_battery_voltage_v"] == 4.699951171875


def test_gauge_rejects_charger_voltage_without_discarding_reported_voltage():
    battery = BatteryTracker()
    battery.observe(3.90, 0)
    gauge_voltage = battery.gauge.filtered_voltage
    battery.observe(4.699951171875, CHARGING | ON_CHARGER)
    assert battery.voltage == 4.699951171875
    assert battery.gauge.filtered_voltage == gauge_voltage
