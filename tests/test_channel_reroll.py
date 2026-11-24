"""Safety and lifecycle tests for dock-only automatic channel reroll.

All tests use in-memory fake switches and robots: no network/power side effects.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cozmo_roverd.config import Config, load
from cozmo_roverd.home_assistant import DockPower, DockPowerError
from cozmo_roverd.main import Bridge
from cozmo_roverd.media import Publisher
from cozmo_roverd import setup


def config(**overrides):
    values = dict(name="test", server_url="ws://127.0.0.1:8080/rover",
                  interface="wlan2", ssid="Cozmo_TEST", password="12345678",
                  video=False, channel_reroll_enabled=True, channel_target=6,
                  home_assistant_url="http://homeassistant.local:8123",
                  home_assistant_token="not-a-real-secret",
                  home_assistant_switch="switch.cozmo_dock")
    values.update(overrides)
    return Config(**values)


class FakeRobot:
    def __init__(self, docked=True):
        self.ready = True
        self.on_charger = docked
        self._dock_confirmed_session = docked
        self.battery = SimpleNamespace(docked=docked)
        self.moving = False
        self.undock_active = False
        self.dock_assist = False
        self.commanded_wheels = (0, 0)
        self.actions = []

    def shutdown_for_channel_reroll(self):
        self.actions.append("shutdown")

    def close(self):
        self.actions.append("close")
        self.ready = False

    def connect(self):
        self.actions.append("connect")
        self.ready = True


class FakePower:
    def __init__(self, state="on"):
        self.current = state
        self.actions = []

    def state(self):
        self.actions.append("state")
        return self.current

    def cycle(self):
        self.actions.append("cycle")


def bridge_case(c, *, docked=True, channels=(1,)):
    bridge = Bridge(c)
    bridge.robot = FakeRobot(docked)
    power = FakePower()
    bridge.dock_power = power
    it = iter(channels)
    bridge.wifi.channel = lambda: next(it)
    bridge.wifi.connect = lambda: power.actions.append("wifi_connect")
    return bridge, power


def test_correct_channel_never_calls_ha_or_shutdown():
    async def scenario():
        bridge, power = bridge_case(config(), channels=(6,))
        await bridge._ensure_channel()
        assert bridge.channel_status == "matched"
        assert bridge.robot.actions == []
        assert power.actions == []
    asyncio.run(scenario())


def test_stale_dock_contact_from_previous_connection_cannot_shutdown():
    async def scenario():
        bridge, power = bridge_case(config(), channels=(1,))
        bridge.robot._dock_confirmed_session = False
        await bridge._ensure_channel()
        assert bridge.channel_status == "waiting_for_dock"
        assert bridge.robot.actions == []
        assert power.actions == []
    asyncio.run(scenario())


def test_undocked_robot_never_calls_ha_or_shutdown():
    async def scenario():
        bridge, power = bridge_case(config(), docked=False, channels=(1,))
        await bridge._ensure_channel()
        assert bridge.channel_status == "waiting_for_dock"
        assert bridge.robot.actions == []
        assert power.actions == []
    asyncio.run(scenario())


def test_unknown_channel_never_calls_ha_or_shutdown():
    async def scenario():
        bridge, power = bridge_case(config(), channels=(None,))
        await bridge._ensure_channel()
        assert bridge.channel_status == "channel_unknown"
        assert bridge.robot.actions == []
        assert power.actions == []
    asyncio.run(scenario())


def test_unavailable_power_switch_blocks_shutdown():
    async def scenario():
        bridge, power = bridge_case(config(), channels=(1,))
        power.current = "off"
        await bridge._ensure_channel()
        assert bridge.channel_status == "home_assistant_unavailable"
        assert bridge.robot.actions == []
        assert "cycle" not in power.actions
    asyncio.run(scenario())


def test_successful_reroll_calls_one_power_cycle(monkeypatch):
    async def no_wait(*args, **kwargs):
        return None

    monkeypatch.setattr("cozmo_roverd.main.asyncio.sleep", no_wait)

    async def scenario():
        # Current ch 1 -> AP vanishes on shutdown -> reconnect on channel 6.
        bridge, power = bridge_case(config(), channels=(1, None, 6))
        bridge.hardware_ready.set()
        await bridge._ensure_channel()
        assert bridge.channel_status == "matched"
        assert bridge.channel_actual == 6
        assert bridge.channel_attempts == 0
        assert bridge.robot.actions == ["shutdown", "close", "connect"]
        assert power.actions == ["state", "cycle", "wifi_connect"]
        assert not bridge._reroll_active
    asyncio.run(scenario())


def test_shutdown_without_wifi_disappearance_never_cuts_power(monkeypatch):
    async def no_wait(*args, **kwargs):
        return None

    monkeypatch.setattr("cozmo_roverd.main.asyncio.sleep", no_wait)

    async def scenario():
        bridge, power = bridge_case(config(), channels=(1,) * 18)
        with pytest.raises(RuntimeError, match="Shutdown did not disconnect"):
            await bridge._ensure_channel()
        assert bridge.robot.actions == ["shutdown"]
        assert "cycle" not in power.actions
        assert bridge.channel_status == "error"
    asyncio.run(scenario())


def test_ganged_power_switch_restored_even_when_off_fails(monkeypatch):
    power = DockPower("http://example.test:8123", "secret", "switch.test")
    actions = []
    monkeypatch.setattr(power, "state", lambda: "on")

    def set_power(on):
        actions.append(on)
        if not on:
            raise DockPowerError("off failed")

    monkeypatch.setattr(power, "set_power", set_power)
    with pytest.raises(DockPowerError):
        power.cycle(off_seconds=0)
    assert actions == [False, True]


def test_offline_video_status_reports_real_cycle_stage():
    label, detail = Publisher._recovery_status({
        "connected": False,
        "hardware_stage": "channel_cycling_dock_power",
        "channel_reroll": {"attempt": 2, "max_attempts": 6},
    }, had_video=True)
    assert label == "CHANGING WIFI CHANNEL"
    assert "2/6" in detail
    assert "dock power" in detail.lower()


def test_setup_preserves_token_and_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "interfaces", lambda: [
        {"name": "wlan2", "selectable": True},
    ])
    previous = {
        "wifi": {"ssid": "Cozmo_TEST", "password": "12345678"},
        "channel_reroll": {
            "enabled": True, "target_channel": 6, "max_attempts": 6,
            "home_assistant_url": "http://homeassistant.local:8123",
            "home_assistant_token": "original-secret",
            "switch_entity": "switch.cozmo_dock",
        },
    }
    values = dict(name="cozmo", description="Cozmo", color="#3ba8ff",
                  private=False, server_url="ws://127.0.0.1:8080/rover",
                  interface="wlan2", ssid="Cozmo_TEST", password="",
                  max_speed=250, fps=15, camera_mode="auto", hud=True,
                  diagnostic_minimal=False, camera_enabled=True, video=True,
                  rtsp_url="", channel_reroll_enabled=True,
                  channel_target=11, channel_max_attempts=6,
                  home_assistant_url="http://homeassistant.local:8123",
                  home_assistant_switch="switch.cozmo_dock",
                  home_assistant_token="")
    saved = setup.validate_payload(values, previous)
    assert saved["channel_reroll"]["home_assistant_token"] == "original-secret"
    target = tmp_path / "config.toml"
    setup.write_config(str(target), saved)
    parsed = load(str(target))
    assert parsed.channel_target == 11
    assert parsed.home_assistant_token == "original-secret"
    assert "original-secret" not in str(setup.public_config(str(target)))


def test_old_configuration_without_reroll_is_disabled(tmp_path):
    target = tmp_path / "config.toml"
    target.write_text(
        '[rover]\nname="cozmo"\nserver_url="ws://localhost:8080/rover"\n'
        '[wifi]\ninterface="wlan2"\nssid="Cozmo_TEST"\npassword="12345678"\n')
    c = load(str(target))
    assert c.channel_reroll_enabled is False
