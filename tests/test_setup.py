"""Tests requiring no robot, systemd or real Wi-Fi hardware."""
import os
import tomllib
from unittest.mock import patch
import pytest

from cozmo_roverd import setup


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
