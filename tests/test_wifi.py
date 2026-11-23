"""Wi-Fi recovery tests do not touch NetworkManager or real adapters."""
from types import SimpleNamespace

from cozmo_roverd import wifi


def config():
    return SimpleNamespace(interface="wlan2", ssid="Cozmo_TEST",
                           password="savedpassword")


def test_unplugged_adapter_is_retried_until_it_returns(monkeypatch):
    manager = wifi.Wifi(config())
    attempts = []
    delays = []

    monkeypatch.setattr(manager, "active", lambda: False)

    def reconnect():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("Wi-Fi device missing")

    monkeypatch.setattr(manager, "connect", reconnect)
    monkeypatch.setattr(wifi.time, "sleep", delays.append)
    manager.wait()
    assert len(attempts) == 3
    assert delays == [3, 3]


def test_active_wifi_is_reused_without_forced_reassociation(monkeypatch):
    manager = wifi.Wifi(config())
    touched = []
    monkeypatch.setattr(manager, "active", lambda: True)
    monkeypatch.setattr(manager, "disconnect_other_cozmo_profiles",
                        lambda: touched.append("clean old"))
    monkeypatch.setattr(manager, "connect",
                        lambda: touched.append("reassociate"))
    manager.wait()
    assert touched == ["clean old"]


def test_activation_is_bounded_and_keeps_device_autoconnect_disabled(monkeypatch):
    manager = wifi.Wifi(config())
    commands = []
    monkeypatch.setattr(manager, "verify", lambda: None)
    monkeypatch.setattr(manager, "disconnect_other_cozmo_profiles", lambda: None)

    def mock_nm(*args, **kwargs):
        commands.append((args, kwargs))
        return ""

    monkeypatch.setattr(wifi, "nm", mock_nm)
    monkeypatch.setattr(wifi.subprocess, "run", lambda *args, **kwargs:
                        SimpleNamespace(returncode=0))
    manager.connect()
    assert (("--wait", "20", "connection", "up",
             manager.profile, "ifname", "wlan2"), {"timeout": 25}) in commands
    assert (("device", "set", "wlan2", "autoconnect", "no"), {}) in commands
