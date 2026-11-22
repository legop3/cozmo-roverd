"""Strict config loading with no external config framework."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
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
    private: bool = False
    rtsp_url: str = ""
    fps: int = 15
    hud: bool = True

def load(path: str) -> Config:
    data = tomllib.loads(Path(path).read_text())
    rover, wifi, media = data.get("rover", {}), data.get("wifi", {}), data.get("media", {})
    c = Config(
        name=str(rover["name"]), server_url=str(rover["server_url"]),
        interface=str(wifi["interface"]), ssid=str(wifi.get("ssid", "")),
        password=str(wifi.get("password", "")),
        description=str(rover.get("description", "Anki Cozmo")),
        color=str(rover.get("color", "#3ba8ff")),
        max_speed=int(rover.get("max_speed", 250)),
        camera_mode=str(media.get("camera_mode", "auto")),
        video=bool(media.get("enabled", True)),
        private=rover.get("private", False),
        rtsp_url=str(media.get("rtsp_url", "")),
        fps=int(media.get("fps", 15)),
        hud=media.get("hud", True),
    )
    if type(c.hud) is not bool:
        raise ValueError("media.hud must be a boolean")
    if type(c.private) is not bool:
        raise ValueError("rover.private must be a boolean")
    url = urlsplit(c.server_url)
    if url.scheme not in ("ws", "wss") or not url.hostname or url.path.rstrip("/") != "/rover":
        raise ValueError("rover.server_url must be ws(s)://host[:port]/rover")
    if not c.name or "/" in c.name or len(c.name) > 64:
        raise ValueError("rover.name must be 1-64 characters without '/'")
    if not c.interface or "/" in c.interface:
        raise ValueError("wifi.interface must be a dedicated Wi-Fi device name")
    if not 1 <= c.max_speed <= 250 or not 1 <= c.fps <= 15:
        raise ValueError("Invalid speed or fps")
    if c.camera_mode not in ("auto", "bright", "lowlight"):
        raise ValueError("Invalid camera mode")
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
