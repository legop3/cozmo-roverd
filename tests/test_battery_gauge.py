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
