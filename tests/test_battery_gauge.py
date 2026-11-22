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
