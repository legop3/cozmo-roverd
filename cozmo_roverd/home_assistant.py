"""Home Assistant dock-power controller. No USB or robot protocol assumptions.

Every command is sent through Home Assistant's switch service API. A successful
HTTP response alone does not prove the switch changed; poll the entity state.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit


class DockPowerError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DockPowerError("Home Assistant redirected an authenticated request")


class DockPower:
    def __init__(self, url: str, token: str, entity: str):
        self.base = url.rstrip("/")
        self.entity = entity
        self.token = token
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _request(self, path: str, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.base + path, data=data, method="POST" if data is not None else "GET",
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=6) as response:
                if response.status < 200 or response.status >= 300:
                    raise DockPowerError(f"Home Assistant returned HTTP {response.status}")
                return json.loads(response.read(1024 * 1024) or b"null")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError,
                ValueError) as exc:
            raise DockPowerError("Home Assistant request failed (" +
                                 type(exc).__name__ + ")") from None

    def state(self) -> str:
        result = self._request("/api/states/" + quote(self.entity, safe=""))
        if not isinstance(result, dict) or result.get("entity_id") != self.entity:
            raise DockPowerError("Home Assistant returned an unexpected switch entity")
        state = result.get("state")
        if state not in ("on", "off"):
            raise DockPowerError(f"Dock power state is not available: {str(state)[:32]}")
        return state

    def set_power(self, on: bool):
        desired = "on" if on else "off"
        self._request("/api/services/switch/turn_" + desired,
                      {"entity_id": self.entity})
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if self.state() == desired:
                return
            time.sleep(0.5)
        raise DockPowerError(f"Dock switch did not report {desired} within 12 seconds")

    def cycle(self, off_seconds: float = 3.0):
        if self.state() != "on":
            raise DockPowerError("Dock switch is not on; refusing to power-cycle")
        # Always try to restore power, including after an OFF API failure.
        try:
            self.set_power(False)
            time.sleep(off_seconds)
        finally:
            self.set_power(True)
