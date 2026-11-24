"""Safety and lifecycle tests for dock-only automatic channel reroll.

All tests use in-memory fake switches and robots: no network/power side effects.
"""
from __future__ import annotations

import asyncio
import time
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
        self.ready = False
        self.last_sensor = time.monotonic()
        self.on_charger = docked
        self._dock_confirmed_session = False
        self.battery = SimpleNamespace(docked=docked)
        self.moving = False
        self.undock_active = False
        self.dock_assist = False
        self.commanded_wheels = (0, 0)
        self.actions = []
        self.docked = docked

    def connect(self, *, inspection=False):
        self.actions.append("inspect" if inspection else "normal_connect")
        self.ready = True
        # Emulate a current-session RobotState charger-contact report.
        self._dock_confirmed_session = self.docked

    def promote_inspection(self):
        self.actions.append("promote")

    def shutdown_for_channel_reroll(self):
        if not self._dock_confirmed_session:
            raise RuntimeError("Refusing to shut down undocked Cozmo")
        self.actions.append("shutdown")

    def close(self):
        self.actions.append("close")
        self.ready = False
        self._dock_confirmed_session = False


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
    bridge.wifi.active = lambda: True
    bridge.wifi.wait = lambda: power.actions.append("wifi_wait")
    return bridge, power


class StopProbe(Exception):
    """End a deliberately indefinite unsafe-channel inspection in tests."""


async def stop_inspection(_delay):
    raise StopProbe()


def test_normal_reconnect_matching_channel_skips_inspection():
    async def scenario():
        bridge, power = bridge_case(config(), channels=(6,))
        async def forbidden_inspection():
            raise AssertionError("Correct channel must not start an inspection session")
        bridge._prepare_channel = forbidden_inspection
        assert await bridge._connect_checked_robot() is True
        assert bridge.channel_status == "matched"
        assert bridge.robot.actions == ["normal_connect"]
        assert power.actions == []
    asyncio.run(scenario())


def test_wrong_channel_still_uses_dock_only_inspection(monkeypatch):
    async def no_wait(*_args, **_kwargs):
        return None

    monkeypatch.setattr("cozmo_roverd.main.asyncio.sleep", no_wait)

    async def scenario():
        bridge, power = bridge_case(config(), channels=(1, 1, 6))
        assert await bridge._connect_checked_robot() is True
        assert bridge.robot.actions == [
            "inspect", "shutdown", "close", "inspect", "promote"
        ]
        assert power.actions == ["state", "cycle", "wifi_wait"]
        assert bridge.channel_status == "matched"
    asyncio.run(scenario())


def test_correct_channel_promotes_only_after_inspection():
    async def scenario():
        bridge, power = bridge_case(config(), channels=(6,))
        await bridge._prepare_channel()
        assert bridge.channel_status == "matched"
        assert not bridge.hardware_ready.is_set()
        assert bridge.robot.actions == ["inspect"]
        assert power.actions == []
        bridge.robot.promote_inspection()
        assert bridge.robot.actions == ["inspect", "promote"]
    asyncio.run(scenario())


def test_stale_dock_contact_cannot_authorize_power_cycle(monkeypatch):
    monkeypatch.setattr("cozmo_roverd.main.asyncio.sleep", stop_inspection)

    async def scenario():
        bridge, power = bridge_case(config(), channels=(1,))
        def never_confirm_dock(*, inspection=False):
            bridge.robot.actions.append("inspect")
            bridge.robot.ready = True
            bridge.robot._dock_confirmed_session = False
        bridge.robot.connect = never_confirm_dock
        with pytest.raises(StopProbe):
            await bridge._prepare_channel()
        assert bridge.robot.actions == ["inspect", "close"]
        assert power.actions == []
        assert bridge.channel_status == "waiting_for_dock"
    asyncio.run(scenario())


def test_undocked_robot_waits_without_power_operations(monkeypatch):
    monkeypatch.setattr("cozmo_roverd.main.asyncio.sleep", stop_inspection)

    async def scenario():
        bridge, power = bridge_case(config(), docked=False, channels=(1,))
        with pytest.raises(StopProbe):
            await bridge._prepare_channel()
        assert bridge.channel_status == "waiting_for_dock"
        assert bridge.robot.actions == ["inspect", "close"]
        assert power.actions == []
        assert not bridge.hardware_ready.is_set()
    asyncio.run(scenario())


def test_unknown_channel_does_not_enable_camera_or_toggle_power(monkeypatch):
    monkeypatch.setattr("cozmo_roverd.main.asyncio.sleep", stop_inspection)

    async def scenario():
        bridge, power = bridge_case(config(), channels=(None,))
        with pytest.raises(StopProbe):
            await bridge._prepare_channel()
        assert bridge.channel_status == "waiting_for_wifi"
        assert bridge.robot.actions == ["inspect", "close"]
        assert power.actions == []
    asyncio.run(scenario())


def test_unavailable_power_switch_blocks_shutdown(monkeypatch):
    monkeypatch.setattr("cozmo_roverd.main.asyncio.sleep", stop_inspection)

    async def scenario():
        bridge, power = bridge_case(config(), channels=(1,))
        power.current = "off"
        with pytest.raises(StopProbe):
            await bridge._prepare_channel()
        assert bridge.channel_status == "home_assistant_unavailable"
        assert bridge.robot.actions == ["inspect", "close"]
        assert power.actions == ["state"]
    asyncio.run(scenario())


def test_successful_reroll_requires_second_inspection_before_promotion(monkeypatch):
    async def no_wait(*args, **kwargs):
        return None

    monkeypatch.setattr("cozmo_roverd.main.asyncio.sleep", no_wait)

    async def scenario():
        bridge, power = bridge_case(config(), channels=(1, 6))
        await bridge._prepare_channel()
        assert bridge.channel_status == "matched"
        assert bridge.channel_actual == 6
        assert bridge.channel_attempts == 1
        assert not bridge.hardware_ready.is_set()
        assert bridge.robot.actions == ["inspect", "shutdown", "close", "inspect"]
        assert power.actions == ["state", "cycle", "wifi_wait"]
        bridge.robot.promote_inspection()
        assert bridge.robot.actions[-1] == "promote"
    asyncio.run(scenario())


def test_failed_client_cleanup_still_wakes_dock(monkeypatch):
    async def no_wait(*args, **kwargs):
        return None

    monkeypatch.setattr("cozmo_roverd.main.asyncio.sleep", no_wait)

    async def scenario():
        bridge, power = bridge_case(config(), channels=(1,))
        def broken_close():
            raise RuntimeError("cleanup error")
        bridge.robot.close = broken_close
        with pytest.raises(RuntimeError, match="cleanup error"):
            await bridge._prepare_channel()
        assert power.actions.count("cycle") == 1
        assert bridge.channel_status == "error"
    asyncio.run(scenario())


def test_preflight_frames_do_not_enter_rtsp():
    async def scenario():
        bridge, _power = bridge_case(config(), channels=(6,))
        frames = []
        analyzed = []
        bridge.publisher = SimpleNamespace(submit=lambda frame: frames.append(frame))
        bridge.robot.queue_camera_analysis = lambda frame: analyzed.append(frame)
        bridge.handle_frame("early-frame")
        assert frames == [] and analyzed == []
        bridge.hardware_ready.set()
        bridge.handle_frame("live-frame")
        assert frames == ["live-frame"] and analyzed == ["live-frame"]
    asyncio.run(scenario())


def test_mrr_grace_timer_is_independent_of_channel_selection():
    async def scenario():
        bridge, _power = bridge_case(config(), channels=(1,))
        bridge._hardware_offline_since = __import__("time").monotonic() - 25
        assert 570 <= bridge._mrr_offline_remaining() <= 576
        bridge.hardware_ready.set()
        assert bridge._mrr_offline_remaining() is None
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
        "channel_reroll": {"attempt": 2},
    }, had_video=True)
    assert label == "CHANGING WIFI CHANNEL"
    assert "2" in detail
    assert "dock power" in detail.lower()


def test_setup_preserves_token_and_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr(setup, "interfaces", lambda: [
        {"name": "wlan2", "selectable": True},
    ])
    previous = {
        "wifi": {"ssid": "Cozmo_TEST", "password": "12345678"},
        "channel_reroll": {
            "enabled": True, "target_channel": 6,
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
                  channel_target=11,
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
