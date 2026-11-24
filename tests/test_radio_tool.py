"""Standalone radio test must join the saved Cozmo Wi-Fi before using PyCozmo."""
from types import SimpleNamespace

import pytest

from tools import test_wifi_channel


class FakeWifi:
    def __init__(self, cfg, *, active=False):
        self.cfg = cfg
        self.is_active = active
        self.calls = []

    def verify(self):
        self.calls.append("verify")

    def active(self):
        self.calls.append("active")
        return self.is_active

    def connect(self):
        self.calls.append("connect")
        self.is_active = True

    def disconnect_other_cozmo_profiles(self):
        self.calls.append("disconnect_other")


@pytest.mark.parametrize("already_connected", [True, False])
def test_test_tool_uses_saved_configured_adapter_without_bouncing_active_wifi(
        monkeypatch, already_connected):
    cfg = SimpleNamespace(interface="wlan2", ssid="Cozmo_ABCDEF")
    instance = FakeWifi(cfg, active=already_connected)
    paths = []
    monkeypatch.setattr(test_wifi_channel, "load", lambda p: paths.append(p) or cfg)
    monkeypatch.setattr(test_wifi_channel, "Wifi", lambda _cfg: instance)

    test_wifi_channel.connect_configured_wifi("/etc/cozmo-roverd.toml")

    assert paths == ["/etc/cozmo-roverd.toml"]
    assert instance.cfg is cfg
    if already_connected:
        assert instance.calls == ["verify", "active", "disconnect_other", "active"]
    else:
        assert instance.calls == ["verify", "active", "connect", "active"]


def test_test_tool_refuses_to_send_if_wifi_does_not_connect(monkeypatch):
    cfg = SimpleNamespace(interface="wlan2", ssid="Cozmo_ABCDEF")
    instance = FakeWifi(cfg)
    instance.connect = lambda: instance.calls.append("connect")
    monkeypatch.setattr(test_wifi_channel, "load", lambda _: cfg)
    monkeypatch.setattr(test_wifi_channel, "Wifi", lambda _: instance)
    with pytest.raises(RuntimeError, match="did not become active"):
        test_wifi_channel.connect_configured_wifi("/etc/cozmo-roverd.toml")


def test_packet_encodings():
    assert test_wifi_channel.packet_for("radio", 5) == (0x07, bytes((1, 5)))
    assert test_wifi_channel.packet_for("wifi-off", 5) == (0xAE, bytes((1,)))
    assert test_wifi_channel.packet_for("wifi-on", 5) == (0xAE, bytes((0,)))
    assert test_wifi_channel.packet_for("sleep", 5) == (0x52, b"")
    assert test_wifi_channel.packet_for("shutdown-legacy", 5) == (0xA9, b"")



def test_channel_from_current_association_without_scan(monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=chr(10).join(("Connected to aa:bb:cc", "freq: 2437", "")))

    monkeypatch.setattr(test_wifi_channel.subprocess, "run", run)
    assert test_wifi_channel.connected_ap_channel("wlan2") == "channel 6 (2437 MHz)"
    assert calls == [["iw", "dev", "wlan2", "link"]]


def test_disconnected_ap_channel(monkeypatch):
    monkeypatch.setattr(test_wifi_channel.subprocess, "run",
                        lambda *_args, **_kwargs: SimpleNamespace(
                            returncode=0, stdout="Not connected."))
    assert test_wifi_channel.connected_ap_channel("wlan2") == "not connected"
