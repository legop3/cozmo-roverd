"""Tests requiring no robot, systemd or real Wi-Fi hardware."""
import os
import tomllib
from unittest.mock import patch
import pytest

from cozmo_roverd import setup


def test_private_rover_saved_and_validated(tmp_path):
    data = {
        "name":"cozmo", "description":"Cozmo", "color":"#3ba8ff",
        "server_url":"ws://localhost:8080/rover", "interface":"wlan1",
        "ssid":"Cozmo_ABC", "password":"password123",
        "max_speed":100, "watchdog_seconds":0.65,
        "video":True, "fps":15, "rtsp_url":"", "private":True,
    }
    with patch.object(setup, "interfaces", return_value=[
            {"name":"wlan1", "selectable":True}]):
        result = setup.validate_payload(data, {})
        assert result["rover"]["private"] is True
        target = tmp_path / "test.toml"
        setup.write_config(str(target), result)
        from cozmo_roverd.config import load
        assert load(str(target)).private is True
        with pytest.raises(ValueError, match="Private rover"):
            setup.validate_payload({**data, "private":"true"}, {})


def test_nmcli_escaped_ssid():
    assert setup.parse_nmcli(r"Cozmo\:123:80:WPA2:*") == ["Cozmo:123", "80", "WPA2", "*"]


def test_redacted_password(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[rover]\nname = "test"\nserver_url = "ws://localhost:8080/rover"\n'
                 '[wifi]\ninterface = "wlan1"\nssid = "Cozmo_123"\npassword = "secretpassword"\n')
    public = setup.public_config(str(f))
    assert public["has_password"] is True
    assert "password" not in public
    assert "secretpassword" not in repr(public)


def test_render_is_parseable_and_atomic(tmp_path):
    target = tmp_path / "config.toml"
    data = {
        "rover":{"name":"cozmo", "description":"Cozmo", "color":"#3ba8ff",
                 "server_url":"ws://localhost:8080/rover", "max_speed":100,
                 "watchdog_seconds":0.65},
        "wifi":{"interface":"wlan1", "ssid":"Cozmo_ABC", "password":"password123"},
        "media":{"enabled":True, "fps":15, "rtsp_url":""}}
    setup.write_config(str(target),data)
    assert setup.read_config(str(target)) == data
    assert os.stat(target).st_mode & 0o777 == 0o600
    data["rover"]["description"] = "Updated"
    setup.write_config(str(target),data)
    assert setup.read_config(str(target))["rover"]["description"] == "Updated"
    assert (tmp_path / "config.toml.bak").exists()


def test_validate_requires_dedicated_interface():
    good = {"name":"cozmo", "description":"Cozmo", "color":"#3ba8ff",
            "server_url":"ws://localhost:8080/rover", "interface":"wlan0",
            "ssid":"Cozmo_ABC", "password":"password123",
            "max_speed":100, "watchdog_seconds":0.65, "video":True,
            "fps":15, "rtsp_url":""}
    with patch.object(setup, "interfaces", return_value=[
            {"name":"wlan0", "selectable":False, "is_uplink":True}]):
        with pytest.raises(ValueError,match="dedicated"):
            setup.validate_payload(good,{})
    with patch.object(setup, "interfaces", return_value=[
            {"name":"wlan0", "selectable":True, "is_uplink":False}]):
        valid = setup.validate_payload(good,{})
        assert valid["wifi"]["ssid"] == "Cozmo_ABC"


def test_existing_password_preserved_only_for_same_network():
    good = {"name":"cozmo", "description":"Cozmo", "color":"#3ba8ff",
            "server_url":"ws://localhost:8080/rover", "interface":"wlan1",
            "ssid":"Cozmo_ABC", "password":"",
            "max_speed":100, "watchdog_seconds":0.65, "video":True,
            "fps":15, "rtsp_url":""}
    existing = {"wifi":{"interface":"wlan1", "ssid":"Cozmo_ABC", "password":"savedpassword"}}
    with patch.object(setup, "interfaces", return_value=[
            {"name":"wlan1", "selectable":True}]):
        assert setup.validate_payload(good,existing)["wifi"]["password"] == "savedpassword"
        with pytest.raises(ValueError,match="password"):
            setup.validate_payload({**good, "ssid":"New SSID"}, existing)


def test_setup_files_packaged():
    from importlib.resources import files
    assert files("cozmo_roverd").joinpath("web/index.html").is_file()
    assert files("cozmo_roverd").joinpath("web/app.js").is_file()


def test_radio_status_reports_disabled_and_hard_block():
    from subprocess import CompletedProcess
    def fake_run(*args, **kwargs):
        if args[:3] == ("nmcli", "radio", "wifi"):
            return CompletedProcess(args, 0, "disabled\n", "")
        if args[:2] == ("rfkill", "list"):
            return CompletedProcess(args, 0, "Soft blocked: yes\nHard blocked: yes\n", "")
        raise AssertionError(args)
    with patch.object(setup, "run", side_effect=fake_run):
        status = setup.radio_status()
        assert status["hard_blocked"]
        assert status["soft_blocked"]
        with pytest.raises(ValueError, match="physical"):
            setup.enable_wifi()


def test_enable_wifi_calls_networkmanager():
    from subprocess import CompletedProcess
    calls = []
    def fake_run(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("nmcli", "radio", "wifi") and len(args) == 3:
            return CompletedProcess(args, 0, "enabled\n", "")
        return CompletedProcess(args, 0, "", "")
    with patch.object(setup, "run", side_effect=fake_run):
        assert setup.enable_wifi()["enabled"]
        assert ("nmcli", "radio", "wifi", "on") in calls


def test_unavailable_adapter_not_mistaken_for_uplink(tmp_path):
    from subprocess import CompletedProcess
    with patch.object(setup.Path, "iterdir", return_value=[tmp_path / "wlan1"]):
        with patch.object(setup, "default_route_devices", return_value=set()):
            with patch.object(setup, "run", return_value=CompletedProcess([], 0, "wifi\n20 (unavailable)\n--\n", "")):
                devices = setup.interfaces()
    assert devices[0]["unavailable"]
    assert devices[0]["selectable"]
    assert devices[0]["state_label"] == "Wi-Fi unavailable"
