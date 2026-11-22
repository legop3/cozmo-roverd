"""LAN-accessible HTTP setup dashboard for headless Fedora hosts.

Runs independently of the rover daemon so setup stays available when the
robot is offline or the main configuration is invalid.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from threading import Lock
from urllib.parse import urlsplit

from .config import load, rtsp_target

LOG = logging.getLogger(__name__)
CONFIG = "/etc/cozmo-roverd.toml"
SERVICE = "cozmo-roverd.service"
SAVE_LOCK = Lock()
IFACE_RE = re.compile(r"^[a-zA-Z0-9_.:-]{1,32}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
COLORS = ("#3ba8ff", "#32c49b", "#ffb44c", "#b48cff", "#ff6f8a", "#f4f4f5")
SPEEDS = (40, 60, 80, 100, 120, 150, 180, 200)
FPS = (5, 10, 15)
WATCHDOG = (0.35, 0.5, 0.65, 0.8, 1.0, 1.5)
MAX_BODY = 8192


def run(*args: str, timeout: int = 8) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          check=False)


def default_route_devices() -> set[str]:
    result = set()
    for proto in ("-4", "-6"):
        proc = run("ip", proto, "route", "show", "default")
        for line in proc.stdout.splitlines():
            tokens = line.split()
            if "dev" in tokens and tokens.index("dev") + 1 < len(tokens):
                result.add(tokens[tokens.index("dev") + 1])
    return result


def radio_status() -> dict:
    radio = run("nmcli", "radio", "wifi")
    text = radio.stdout.strip().lower()
    rf = run("rfkill", "list", "wifi")
    lines = rf.stdout.lower()
    return {
        "enabled": text == "enabled",
        "state": text or "unknown",
        "hard_blocked": "hard blocked: yes" in lines,
        "soft_blocked": "soft blocked: yes" in lines,
    }


def enable_wifi() -> dict:
    info = radio_status()
    if info["hard_blocked"]:
        raise ValueError("Wi-Fi is blocked by a physical switch or laptop hotkey. Unlock it on the computer.")
    p = run("nmcli", "radio", "wifi", "on")
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or "Could not enable Wi-Fi")
    # A radio can take a moment to transition to usable state.
    return radio_status()


def interfaces() -> list[dict]:
    devices = []
    uplinks = default_route_devices()
    root = Path("/sys/class/net")
    for dev in sorted(root.iterdir()) if root.exists() else []:
        name = dev.name
        if not IFACE_RE.fullmatch(name):
            continue
        p = run("nmcli", "-g", "GENERAL.TYPE,GENERAL.STATE,GENERAL.CONNECTION",
                "device", "show", name)
        if p.returncode:
            continue
        lines = p.stdout.splitlines()
        if not lines or lines[0].strip() != "wifi":
            continue
        status = lines[1].strip() if len(lines) > 1 else "unknown"
        conn = lines[2].strip() if len(lines) > 2 else ""
        devices.append({"name": name, "state": status, "connection": conn,
                        "is_uplink": name in uplinks, "selectable": name not in uplinks,
                        "unavailable": status.startswith("20"), "state_label":
                            "Wi-Fi unavailable" if status.startswith("20") else
                            "Ready to scan" if status.startswith("30") else status})
    return devices


def parse_nmcli(line: str) -> list[str]:
    """Colon delimiter with NMCLI backslash escaping."""
    fields, part, escaping = [], "", False
    for char in line:
        if escaping:
            part += char
            escaping = False
        elif char == "\\":
            escaping = True
        elif char == ":":
            fields.append(part)
            part = ""
        else:
            part += char
    fields.append(part)
    return fields


def wifi_scan(interface: str) -> list[dict]:
    if not IFACE_RE.fullmatch(interface):
        raise ValueError("Invalid adapter")
    known = {d["name"]: d for d in interfaces()}
    if interface not in known:
        raise ValueError("Choose a Wi-Fi adapter")
    if known[interface]["is_uplink"]:
        raise ValueError("This adapter provides the computer's network connection. Choose a different one.")
    p = run("nmcli", "--colors", "no", "-t", "--escape", "yes",
            "-f", "SSID,SIGNAL,SECURITY,IN-USE",
            "device", "wifi", "list", "ifname", interface, "--rescan", "yes", timeout=22)
    if p.returncode:
        raise RuntimeError(p.stderr.strip() or "Wi-Fi scan failed")
    found = {}
    for line in p.stdout.splitlines():
        cols = parse_nmcli(line)
        if len(cols) < 4 or not cols[0].strip():
            continue
        ssid, strength, security, active = cols[:4]
        try:
            signal = max(0, min(100, int(strength)))
        except ValueError:
            signal = 0
        if ssid not in found or signal > found[ssid]["signal"]:
            found[ssid] = {"ssid": ssid, "signal": signal, "security": security,
                           "connected": active.strip() == "*",
                           "cozmo": ssid.lower().startswith("cozmo")}
    return sorted(found.values(), key=lambda n: (not n["cozmo"], -n["signal"], n["ssid"].lower()))


def read_config(path: str) -> dict:
    try:
        return tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        LOG.warning("Config cannot be read: %s", exc)
        return {}


def public_config(path: str) -> dict:
    d = read_config(path)
    rover, wifi, media = d.get("rover", {}), d.get("wifi", {}), d.get("media", {})
    return {
        "name": rover.get("name", "cozmo"),
        "description": rover.get("description", "Cozmo"),
        "color": rover.get("color", "#3ba8ff"),
        "server_url": rover.get("server_url", ""),
        "interface": wifi.get("interface", ""),
        "ssid": wifi.get("ssid", ""),
        "has_password": bool(wifi.get("password")),
        "max_speed": rover.get("max_speed", 100),
        "watchdog_seconds": rover.get("watchdog_seconds", 0.65),
        "video": media.get("enabled", True),
        "fps": media.get("fps", 15),
        "rtsp_url": media.get("rtsp_url", ""),
        "configured": bool(wifi.get("interface") and wifi.get("ssid") and wifi.get("password")
                           and rover.get("server_url") and "YOUR-" not in rover.get("server_url", "")),
    }


def validate_payload(payload: dict, current: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Invalid configuration")
    allowed = {"name", "description", "color", "server_url", "interface", "ssid", "password",
               "max_speed", "watchdog_seconds", "video", "fps", "rtsp_url"}
    if set(payload) - allowed:
        raise ValueError("Unknown configuration field")
    name = str(payload.get("name", "")).strip()
    if not NAME_RE.fullmatch(name):
        raise ValueError("Rover name must use 1–48 letters, digits, '-' or '_'")
    desc = str(payload.get("description", "")).strip()
    if not desc or len(desc) > 100:
        raise ValueError("Description must be 1–100 characters")
    color = payload.get("color")
    if color not in COLORS:
        raise ValueError("Choose a color from the palette")
    interface = str(payload.get("interface", ""))
    devs = {d["name"]: d for d in interfaces()}
    if interface not in devs or not devs[interface]["selectable"]:
        raise ValueError("Choose a dedicated Wi-Fi adapter; the active uplink is protected")
    ssid = payload.get("ssid")
    if not isinstance(ssid, str) or not ssid or len(ssid.encode("utf-8")) > 32:
        raise ValueError("Select a Wi-Fi network with a valid SSID")
    oldwifi = current.get("wifi", {})
    password = payload.get("password", "")
    if not password and ssid == oldwifi.get("ssid") and interface == oldwifi.get("interface"):
        password = oldwifi.get("password", "")
    if not isinstance(password, str) or len(password) < 8 or len(password) > 63:
        raise ValueError("Enter the Cozmo Wi-Fi password (8–63 characters)")
    url = str(payload.get("server_url", "")).strip()
    parsed = urlsplit(url)
    try:
        _port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid MRR server port") from exc
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname or parsed.path.rstrip("/") != "/rover":
        raise ValueError("MRR URL must end in /rover and begin with ws:// or wss://")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Do not include credentials, query strings or fragments in MRR URL")
    max_speed = payload.get("max_speed")
    fps = payload.get("fps")
    watchdog = payload.get("watchdog_seconds")
    if type(max_speed) is not int or max_speed not in SPEEDS:
        raise ValueError("Choose a speed from the dropdown")
    if type(fps) is not int or fps not in FPS:
        raise ValueError("Choose an FPS value from the dropdown")
    if type(watchdog) not in (float, int) or float(watchdog) not in WATCHDOG:
        raise ValueError("Choose a watchdog timeout from the dropdown")
    video = payload.get("video")
    if type(video) is not bool:
        raise ValueError("Video setting is invalid")
    rtsp = str(payload.get("rtsp_url", "")).strip()
    if rtsp:
        target = urlsplit(rtsp)
        if target.scheme != "rtsp" or not target.hostname or target.username or target.password:
            raise ValueError("RTSP override must be rtsp://host:port/path (no credentials)")
    return {"rover": {"name": name, "description": desc, "color": color,
                      "server_url": url, "max_speed": max_speed,
                      "watchdog_seconds": watchdog},
            "wifi": {"interface": interface, "ssid": ssid, "password": password},
            "media": {"enabled": video, "fps": fps, "rtsp_url": rtsp}}


def render_toml(data: dict) -> str:
    output = ["# Generated by Cozmo RoverD Setup. Use the web UI to edit.\n"]
    for section in ("rover", "wifi", "media"):
        output.append(f"\n[{section}]\n")
        for key, value in data[section].items():
            # JSON strings are also valid TOML basic strings.
            if isinstance(value, bool):
                val = "true" if value else "false"
            elif isinstance(value, (int, float)):
                val = str(value)
            else:
                val = json.dumps(value, ensure_ascii=True)
            output.append(f"{key} = {val}\n")
    return "".join(output)


def write_config(path: str, config: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    new = render_toml(config)
    # Validate generated data with the daemon's own config loader before replacement.
    fd, tmp = tempfile.mkstemp(prefix=".cozmo-roverd-", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(new)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        load(tmp)
        if target.exists():
            backup = target.with_name(target.name + ".bak")
            shutil.copy2(target, backup)
            os.chmod(backup, 0o600)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def service_status() -> dict:
    active = run("systemctl", "is-active", SERVICE)
    enabled = run("systemctl", "is-enabled", SERVICE)
    return {"active": active.stdout.strip() == "active",
            "enabled": enabled.stdout.strip() == "enabled",
            "state": active.stdout.strip() or "unknown"}


def server_probe(url: str) -> dict:
    p = urlsplit(url)
    try:
        port = p.port or (443 if p.scheme == "wss" else 80)
        if p.scheme not in ("ws", "wss") or not p.hostname or p.path.rstrip("/") != "/rover":
            raise ValueError("Enter an MRR WebSocket URL ending with /rover")
        with socket.create_connection((p.hostname, port), timeout=3):
            return {"reachable": True, "message": "Server port is reachable (rover protocol not yet tested)"}
    except (OSError, ValueError) as exc:
        return {"reachable": False, "message": str(exc)[:180]}


def recent_logs() -> list[str]:
    p = run("journalctl", "-u", SERVICE, "-n", "18", "--no-pager", "--output=cat")
    # Avoid displaying credential-bearing NM logs: only bridge service logs.
    return p.stdout.splitlines()[-18:] if p.returncode == 0 else []


class Handler(BaseHTTPRequestHandler):
    server_version = "CozmoSetup/1"
    config_path = CONFIG

    def log_message(self, fmt, *args):
        LOG.info("%s %s", self.address_string(), fmt % args)

    def send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def origin_valid(self) -> bool:
        # Require browser actions to originate from this page, not another site.
        # No login is required on the trusted LAN.
        origin = self.headers.get("Origin", "")
        parsed = urlsplit(origin)
        return (parsed.scheme == "http" and
                parsed.netloc.lower() == self.headers.get("Host", "").lower() and
                not parsed.path and not parsed.query and not parsed.fragment)

    def do_GET(self):
        try:
            if self.path == "/":
                body = files("cozmo_roverd").joinpath("web/index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/app.js":
                body = files("cozmo_roverd").joinpath("web/app.js").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/config":
                self.send_json(200, {"config": public_config(self.config_path),
                                     "options": {"speeds": SPEEDS, "fps": FPS, "watchdog": WATCHDOG, "colors": COLORS}})
            elif self.path == "/api/interfaces":
                self.send_json(200, {"interfaces": interfaces(), "radio": radio_status()})
            elif self.path == "/api/status":
                d = public_config(self.config_path)
                self.send_json(200, {"service": service_status(),
                                     "configured": d["configured"],
                                     "wifi": [{"name": i["name"], "connection": i["connection"]}
                                              for i in interfaces() if i["name"] == d["interface"]],
                                     "logs": recent_logs()})
            else:
                self.send_json(404, {"error": "Not found"})
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            self.send_json(500, {"error": str(exc)[:200]})

    def do_POST(self):
        if not self.origin_valid():
            self.send_json(403, {"error": "Request must come from this setup page"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                raise ValueError("Request too large or empty")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError("Expected JSON object")
            if self.path == "/api/enable-wifi":
                self.send_json(200, {"radio": enable_wifi()})
            elif self.path == "/api/scan":
                self.send_json(200, {"networks": wifi_scan(data.get("interface", ""))})
            elif self.path == "/api/probe":
                self.send_json(200, server_probe(str(data.get("url", ""))))
            elif self.path == "/api/save":
                with SAVE_LOCK:
                    saved = validate_payload(data, read_config(self.config_path))
                    write_config(self.config_path, saved)
                action = run("systemctl", "enable", SERVICE)
                if action.returncode:
                    raise RuntimeError("Configuration saved, but enabling service failed: " + action.stderr.strip()[:120])
                # Use --no-block to avoid blocking this web request while service stops.
                action = run("systemctl", "restart", "--no-block", SERVICE)
                if action.returncode:
                    raise RuntimeError("Configuration saved, but service restart failed: " + action.stderr.strip()[:120])
                self.send_json(200, {"saved": True, "message": "Saved. Bridge is restarting with the new settings."})
            elif self.path == "/api/stop":
                action = run("systemctl", "stop", "--no-block", SERVICE)
                if action.returncode:
                    raise RuntimeError(action.stderr.strip() or "Unable to stop")
                self.send_json(200, {"message": "Bridge is stopping. Setup remains available."})
            elif self.path == "/api/start":
                if not public_config(self.config_path)["configured"]:
                    raise ValueError("Complete setup before starting")
                action = run("systemctl", "start", "--no-block", SERVICE)
                if action.returncode:
                    raise RuntimeError(action.stderr.strip() or "Unable to start")
                self.send_json(200, {"message": "Bridge is starting."})
            else:
                self.send_json(404, {"error": "Unknown action"})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": str(exc)[:200]})
        except (OSError, subprocess.TimeoutExpired, RuntimeError) as exc:
            self.send_json(500, {"error": str(exc)[:200]})


def main():
    parser = argparse.ArgumentParser(description="Cozmo RoverD setup UI")
    parser.add_argument("--config", default=CONFIG)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind", default="0.0.0.0")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("Invalid port")
    Handler.config_path = args.config
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    LOG.info("Setup UI listening over HTTP on %s:%s", args.bind, args.port)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
