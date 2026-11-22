"""Deterministic voltage-only fuel-gauge scenarios, no robot required."""
from cozmo_roverd.battery_gauge import VoltageFuelGauge


def tick(gauge, voltage, start, seconds, **conditions):
    for offset in range(seconds):
        gauge.observe(voltage, now=start + offset, **conditions)


def test_wheel_voltage_sag_is_not_lost_charge():
    g = VoltageFuelGauge()
    tick(g, 3.90, 0, 90)
    before = g.soc
    tick(g, 3.72, 90, 8, moving=True)
    assert g.soc > before - 7
    assert 0 <= g.learned_sag_v <= 0.18
    tick(g, 3.90, 98, 15)
    assert g.soc > before - 7
    assert g.mode == "rest"


def test_charger_voltage_cannot_immediately_set_100_percent():
    g = VoltageFuelGauge()
    tick(g, 3.80, 0, 60)
    before = g.soc
    g.observe(4.18, now=60, charging=True, docked=True)
    assert g.soc <= before + 1
    tick(g, 4.18, 61, 60, charging=True, docked=True)
    assert g.soc < before + 2
    assert g.mode == "charging"


def test_dock_contact_without_charging_does_not_increase_charge():
    g = VoltageFuelGauge()
    tick(g, 3.80, 0, 60)
    before = g.soc
    tick(g, 4.15, 60, 90, docked=True)
    assert g.soc == before


def test_restart_preserves_recent_estimate_and_calibration():
    g = VoltageFuelGauge()
    tick(g, 3.86, 0, 60)
    g.learned_sag_v = 0.12
    snapshot = g.export(now_wall=1000.0)
    restored = VoltageFuelGauge()
    assert restored.restore(snapshot, now_wall=1030.0)
    restored.observe(3.86, now=0.0)
    assert abs(restored.soc - g.soc) < 1
    assert restored.learned_sag_v == 0.12
    stale = VoltageFuelGauge()
    assert not stale.restore(snapshot, now_wall=6000.0)


def test_resting_voltage_gradually_corrects_estimate():
    g = VoltageFuelGauge()
    tick(g, 3.75, 0, 80)
    before = g.soc
    tick(g, 3.95, 80, 360)
    assert g.soc > before + 20
    assert g.soc < 80


def test_low_warning_uses_resting_3_3v_not_motor_sag():
    g = VoltageFuelGauge()
    tick(g, 3.42, 0, 180)
    assert g.low_confirmed is False
    tick(g, 3.18, 180, 180)
    assert g.low_confirmed is True
    tick(g, 3.68, 360, 240)
    assert g.low_confirmed is False


def test_short_motor_sag_never_confirms_low_voltage():
    g = VoltageFuelGauge()
    tick(g, 3.65, 0, 80)
    tick(g, 3.1, 80, 2, moving=True)
    assert g.low_confirmed is False
    tick(g, 3.65, 82, 40)
    assert g.low_confirmed is False


def test_sustained_loaded_voltage_warns_before_power_loss():
    g = VoltageFuelGauge()
    tick(g, 3.80, 0, 100)
    tick(g, 3.40, 100, 8, moving=True)
    assert g.low_confirmed is False
    g.observe(3.40, now=108, moving=True)
    assert g.low_confirmed is True
    assert g.critical_confirmed is False
    # A short recovery does not hide a real low-battery warning.
    tick(g, 3.75, 109, 3, moving=True)
    assert g.low_confirmed is True


def test_sustained_critical_loaded_voltage_triggers_fast():
    g = VoltageFuelGauge()
    tick(g, 3.80, 0, 90)
    tick(g, 3.14, 90, 3, moving=True)
    assert g.low_confirmed is False
    g.observe(3.14, now=93, moving=True)
    assert g.critical_confirmed is True
    assert g.low_confirmed is True


def test_low_voltage_recovery_requires_rest_not_one_motor_spike():
    g = VoltageFuelGauge()
    tick(g, 3.85, 0, 50)
    tick(g, 3.40, 50, 15, moving=True)
    assert g.low_confirmed is True
    tick(g, 3.90, 65, 8, moving=True)
    assert g.low_confirmed is True
    tick(g, 3.95, 73, 250)
    assert g.low_confirmed is False


# End-to-end Cozmo battery tracker -> MRR needsCharge transition tests.
from cozmo_roverd.robot_packet import (
    BatteryTracker, CHARGING, ON_CHARGER, MOVING,
    RECOVERY_WITH_VOLTAGE_SECONDS, RECOVERY_NO_VOLTAGE_SECONDS,
)


def _low_battery_tracker():
    battery = BatteryTracker()
    for t in range(160):
        battery.observe(3.18, 0, now=float(t))
    assert battery.low is True
    assert battery.needs_charge is True
    return battery


def test_recharge_unlocks_without_waiting_for_estimated_soc():
    b = _low_battery_tracker()
    start_pct = b.percent
    for t in range(160, 160 + RECOVERY_WITH_VOLTAGE_SECONDS + 2):
        b.observe(3.82, ON_CHARGER | CHARGING, now=float(t))
    assert not b.needs_charge
    assert b.percent is not None
    assert b.gauge.low_confirmed is False
    # Old low-battery latch must not immediately relock on undocking.
    b.observe(3.80, MOVING, now=float(t + 1), moving=True)
    assert b.needs_charge is False


def test_docked_but_not_charging_never_earns_unlock():
    b = _low_battery_tracker()
    for t in range(160, 160 + RECOVERY_NO_VOLTAGE_SECONDS + 30):
        b.observe(4.10, ON_CHARGER, now=float(t))
    assert b.needs_charge
    assert b.diagnostic()["recovery_charge_seconds"] == 0


def test_missing_docked_voltage_still_recovers_after_long_confirmed_charge():
    b = _low_battery_tracker()
    for t in range(160, 160 + RECOVERY_NO_VOLTAGE_SECONDS + 2):
        b.observe(0.0, ON_CHARGER | CHARGING, now=float(t))
    assert b.needs_charge is False
    assert b.percent is not None


def test_short_charging_does_not_clear_low_battery_lock():
    b = _low_battery_tracker()
    for t in range(160, 160 + RECOVERY_WITH_VOLTAGE_SECONDS - 5):
        b.observe(3.80, ON_CHARGER | CHARGING, now=float(t))
    assert b.needs_charge
    assert b.gauge.low_confirmed


def test_charger_disconnect_resets_recovery_credit():
    b = _low_battery_tracker()
    for t in range(160, 160 + 500):
        b.observe(3.80, ON_CHARGER | CHARGING, now=float(t))
    for t in range(660, 695):
        b.observe(3.70, 0, now=float(t))
    assert b.diagnostic()["recovery_charge_seconds"] == 0
    for t in range(695, 695 + 410):
        b.observe(3.80, ON_CHARGER | CHARGING, now=float(t))
    assert b.needs_charge  # cannot combine two separated charges


def test_stale_robotstate_does_not_credit_missing_charging_time():
    b = _low_battery_tracker()
    for t in range(160, 510):
        b.observe(3.80, ON_CHARGER | CHARGING, now=float(t))
    b.observe(3.80, ON_CHARGER | CHARGING, now=2000.0)
    assert b.diagnostic()["recovery_charge_seconds"] == 0
    assert b.needs_charge


def test_recent_dangerously_low_voltage_blocks_timed_unlock():
    b = _low_battery_tracker()
    for t in range(160, 160 + RECOVERY_NO_VOLTAGE_SECONDS + 20):
        b.observe(3.20, ON_CHARGER | CHARGING, now=float(t))
    assert b.needs_charge


def test_short_charger_pause_keeps_credit_but_does_not_earn_more():
    b = _low_battery_tracker()
    for t in range(160, 160 + 600):
        b.observe(3.80, ON_CHARGER | CHARGING, now=float(t))
    credit = b.diagnostic()["recovery_charge_seconds"]
    for t in range(760, 790):
        b.observe(3.80, ON_CHARGER, now=float(t))
    assert b.diagnostic()["recovery_charge_seconds"] == credit
    for t in range(790, 790 + 305):
        b.observe(3.80, ON_CHARGER | CHARGING, now=float(t))
    assert b.needs_charge is False


def test_persisted_low_battery_lock_unlocks_with_only_charger_flags(monkeypatch):
    import time
    original = _low_battery_tracker()
    saved = original.gauge.export()
    saved.update(original.charge_state_snapshot())
    fresh = BatteryTracker()
    assert fresh.restore_charge_state(saved)
    assert fresh.needs_charge is True
    assert fresh.battery_state()["needsCharge"] is True
    # Simulate Cozmo returning no valid voltage after a daemon restart.
    for t in range(RECOVERY_NO_VOLTAGE_SECONDS + 2):
        fresh.observe(0.0, ON_CHARGER | CHARGING, now=float(t))
    assert fresh.percent is None
    assert fresh.battery_state()["needsCharge"] is False


def test_charge_lock_is_not_cleared_by_a_stale_saved_snapshot():
    original = _low_battery_tracker()
    saved = original.gauge.export()
    saved.update(original.charge_state_snapshot())
    saved["saved_at"] -= 4000
    fresh = BatteryTracker()
    assert not fresh.restore_charge_state(saved)
    assert fresh.battery_state()["needsCharge"] is None


def test_no_charge_credit_during_no_telemetry_across_reconnect():
    b = _low_battery_tracker()
    for t in range(160, 560):
        b.observe(3.83, ON_CHARGER | CHARGING, now=float(t))
    assert b.diagnostic()["recovery_charge_seconds"] > 300
    b.observe(3.83, ON_CHARGER | CHARGING, now=3600.0)
    assert b.diagnostic()["recovery_charge_seconds"] == 0
    assert b.needs_charge is True
