"""Strict config loading with no external config framework."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
import re
import tomllib

@dataclass(frozen=True)
class Config:
    name: str
    server_url: str
    interface: str
    ssid: str
    password: str
    description: str = "Anki Cozmo"
    color: str = "#3ba8ff"
    max_speed: int = 250
    camera_mode: str = "auto"
    video: bool = True
    camera_enabled: bool = True
    diagnostic_minimal: bool = False
    private: bool = False
    rtsp_url: str = ""
    fps: int = 15
    hud: bool = True
    channel_reroll_enabled: bool = False
    channel_target: int = 1
    home_assistant_url: str = ""
    home_assistant_token: str = ""
    home_assistant_switch: str = ""
    channel_max_attempts: int = 6

def load(path: str) -> Config:
    data = tomllib.loads(Path(path).read_text())
    rover, wifi, media = data.get("rover", {}), data.get("wifi", {}), data.get("media", {})
    reroll = data.get("channel_reroll", {})
    c = Config(
        name=str(rover["name"]), server_url=str(rover["server_url"]),
        interface=str(wifi["interface"]), ssid=str(wifi.get("ssid", "")),
        password=str(wifi.get("password", "")),
        description=str(rover.get("description", "Anki Cozmo")),
        color=str(rover.get("color", "#3ba8ff")),
        max_speed=int(rover.get("max_speed", 250)),
        # Legacy bright/lowlight settings now use fully adaptive auto.
        camera_mode="auto",
        video=bool(media.get("enabled", True)),
        camera_enabled=media.get("camera_enabled", True),
        diagnostic_minimal=media.get("diagnostic_minimal", False),
        private=rover.get("private", False),
        rtsp_url=str(media.get("rtsp_url", "")),
        fps=int(media.get("fps", 15)),
        hud=media.get("hud", True),
        channel_reroll_enabled=reroll.get("enabled", False),
        channel_target=reroll.get("target_channel", 1),
        home_assistant_url=str(reroll.get("home_assistant_url", "")).strip(),
        home_assistant_token=str(reroll.get("home_assistant_token", "")),
        home_assistant_switch=str(reroll.get("switch_entity", "")).strip(),
        channel_max_attempts=reroll.get("max_attempts", 6),
    )
    if type(c.diagnostic_minimal) is not bool:
        raise ValueError("media.diagnostic_minimal must be a boolean")
    if type(c.camera_enabled) is not bool:
        raise ValueError("media.camera_enabled must be a boolean")
    if type(c.hud) is not bool:
        raise ValueError("media.hud must be a boolean")
    if type(c.private) is not bool:
        raise ValueError("rover.private must be a boolean")
    if type(c.channel_reroll_enabled) is not bool:
        raise ValueError("channel_reroll.enabled must be a boolean")
    if type(c.channel_target) is not int or c.channel_target not in (1, 6, 11):
        raise ValueError("channel_reroll.target_channel must be 1, 6 or 11")
    if type(c.channel_max_attempts) is not int or not 1 <= c.channel_max_attempts <= 20:
        raise ValueError("channel_reroll.max_attempts must be 1–20")
    if c.channel_reroll_enabled:
        ha = urlsplit(c.home_assistant_url)
        if (ha.scheme not in ("http", "https") or not ha.hostname
                or ha.username or ha.password or ha.query or ha.fragment
                or ha.path not in ("", "/")):
            raise ValueError("Home Assistant URL must be http(s)://host[:port]")
        if not c.home_assistant_token.strip():
            raise ValueError("Home Assistant token required when channel reroll is enabled")
        if not re.fullmatch(r"switch\\.[a-z0-9_]+", c.home_assistant_switch):
            raise ValueError("Choose a valid Home Assistant switch entity (switch.name)")
    url = urlsplit(c.server_url)
    if url.scheme not in ("ws", "wss") or not url.hostname or url.path.rstrip("/") != "/rover":
        raise ValueError("rover.server_url must be ws(s)://host[:port]/rover")
    if not c.name or "/" in c.name or len(c.name) > 64:
        raise ValueError("rover.name must be 1-64 characters without '/'")
    if not c.interface or "/" in c.interface:
        raise ValueError("wifi.interface must be a dedicated Wi-Fi device name")
    if not 1 <= c.max_speed <= 250 or not 1 <= c.fps <= 15:
        raise ValueError("Invalid speed or fps")
    if c.video and c.rtsp_url and urlsplit(c.rtsp_url).scheme != "rtsp":
        raise ValueError("media.rtsp_url must be an RTSP URL")
    return c

def rtsp_target(c: Config) -> str:
    if c.rtsp_url:
        return c.rtsp_url
    host = urlsplit(c.server_url).hostname
    # Bracket IPv6 literals in RTSP URLs.
    if ":" in host:
        host = f"[{host}]"
    return f"rtsp://{host}:8554/{c.name}"
