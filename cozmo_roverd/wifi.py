"""Give one selected interface exclusively to a non-default-route Cozmo connection."""
from __future__ import annotations
import logging
import subprocess
import time
from .config import Config

LOG = logging.getLogger(__name__)
PROFILE = "cozmo-roverd"

def nm(*args: str, timeout: int = 30) -> str:
    proc = subprocess.run(["nmcli", *args], capture_output=True, text=True,
                          timeout=timeout, check=False)
    if proc.returncode:
        raise RuntimeError(f"nmcli {' '.join(args[:3])}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()

class Wifi:
    def __init__(self, config: Config):
        self.cfg = config
        self.profile = f"{PROFILE}-{config.interface}"

    def verify(self):
        kind = nm("-g", "GENERAL.TYPE", "device", "show", self.cfg.interface)
        if kind.strip() != "wifi":
            raise RuntimeError(f"{self.cfg.interface} is not a Wi-Fi adapter ({kind})")
        if not self.cfg.ssid or not self.cfg.password:
            raise RuntimeError("Set wifi.ssid and wifi.password from Cozmo's display")
        # Do not permit the uplink and the Cozmo adapter to be the same interface.
        routes = subprocess.run(["ip", "-4", "route", "show", "default", "dev", self.cfg.interface],
                                capture_output=True, text=True, check=False)
        if routes.stdout.strip():
            raise RuntimeError(f"{self.cfg.interface} is currently a default-route interface; refusing to take over")

    def connect(self):
        self.verify()
        # Existing non-Cozmo profiles are left untouched. This explicitly activates
        # only our interface-bound profile and prevents DHCP from replacing the uplink.
        existing = subprocess.run(["nmcli", "-g", "connection.id", "connection", "show", self.profile],
                                  capture_output=True, text=True, check=False)
        if existing.returncode:
            nm("connection", "add", "type", "wifi", "ifname", self.cfg.interface,
               "con-name", self.profile, "ssid", self.cfg.ssid, "802-11-wireless.mode", "infrastructure")
        nm("connection", "modify", self.profile,
           "connection.interface-name", self.cfg.interface,
           "connection.autoconnect", "no",
           "802-11-wireless.ssid", self.cfg.ssid,
           "802-11-wireless-security.key-mgmt", "wpa-psk",
           "802-11-wireless-security.psk", self.cfg.password,
           "ipv4.method", "auto", "ipv4.never-default", "yes",
           "ipv4.route-metric", "900", "ipv6.method", "disabled")
        LOG.info("Activating Cozmo-only Wi-Fi profile on %s", self.cfg.interface)
        nm("device", "set", self.cfg.interface, "managed", "yes")
        # Disable device autoconnect: do not fall back to personal/home Wi-Fi profiles.
        nm("device", "set", self.cfg.interface, "autoconnect", "no")
        nm("connection", "up", self.profile, "ifname", self.cfg.interface, timeout=55)

    def wait(self):
        while True:
            try:
                self.connect()
                return
            except Exception as exc:
                LOG.warning("Cozmo Wi-Fi unavailable: %s; retry in 5 seconds", exc)
                time.sleep(5)

    def active(self) -> bool:
        try:
            return nm("-g", "GENERAL.CONNECTION", "device", "show", self.cfg.interface) == self.profile
        except Exception:
            return False
