"""Supervise dedicated Cozmo Wi-Fi, PyCozmo, MRR WebSocket and RTSP publishing."""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import signal
import time
from pathlib import Path
import websockets
from .config import load, rtsp_target
from .media import Publisher
from .protocol import ack, bounded_speed, finite, hello, sensor, state
from .robot import Robot
from .wifi import Wifi

LOG = logging.getLogger("cozmo-roverd")

class Bridge:
    def __init__(self, config):
        self.cfg = config
        self.stop_event = asyncio.Event()
        self.hardware_ready = asyncio.Event()
        self.outbox = asyncio.Queue(maxsize=64)
        self.loop = asyncio.get_running_loop()
        self.wifi = Wifi(config)
        self.publisher = Publisher(rtsp_target(config), config.fps) if config.video else None
        self.robot = Robot(max_speed=config.max_speed, on_state=self.publish_state,
                           on_sensor=self.publish_sensor, on_frame=self.handle_frame)
        self.head_degrees = 10.0

    def enqueue(self, data):
        # Called from event-dispatch threads. Never block PyCozmo's receive loop.
        def add():
            if self.outbox.full():
                try: self.outbox.get_nowait()
                except asyncio.QueueEmpty: pass
            self.outbox.put_nowait(data)
        self.loop.call_soon_threadsafe(add)

    def publish_state(self):
        self.enqueue(state(charging=self.robot.charging, headlight=self.robot.headlight))

    def publish_sensor(self):
        if time.monotonic() - self.robot.last_sensor > 5:
            return
        c = self.robot.client
        self.enqueue(sensor(voltage=self.robot.voltage, cliff=self.robot.cliff,
                            left=float(c.left_wheel_speed.mmps) if c else None,
                            right=float(c.right_wheel_speed.mmps) if c else None))

    def handle_frame(self, image):
        if self.publisher:
            self.publisher.submit(image)

    def execute(self, msg):
        cmd = msg.get("type")
        if cmd == "drive":
            payload = msg.get("driveDirect")
            if not isinstance(payload, dict): raise ValueError("driveDirect missing")
            left = bounded_speed(payload.get("left"), self.cfg.max_speed)
            right = bounded_speed(payload.get("right"), self.cfg.max_speed)
            self.robot.drive(left, right)
        elif cmd == "servo":
            payload = msg.get("servo")
            if not isinstance(payload, dict): raise ValueError("servo payload missing")
            if "angle" in payload:
                target = finite(payload["angle"])
            elif "nudge" in payload:
                target = self.head_degrees + finite(payload["nudge"])
            else:
                raise ValueError("raw pulse servo control not available")
            self.head_degrees = max(-25, min(44, target))
            self.robot.head(self.head_degrees)
        elif cmd == "headlight":
            payload = msg.get("headlight")
            action = payload.get("action") if isinstance(payload, dict) else None
            if action not in ("on", "off", "toggle"): raise ValueError("invalid light action")
            self.robot.light(not self.robot.headlight if action == "toggle" else action == "on")
        elif cmd == "peripheral":
            p = msg.get("peripheral")
            if not isinstance(p, dict) or p.get("id") != "cozmo":
                raise ValueError("unknown peripheral")
            control, value = p.get("control"), p.get("value")
            if control == "lift" and type(value) is int and 0 <= value <= 100:
                self.robot.lift(value)
            elif control == "light" and type(value) is bool:
                self.robot.light(value)
            elif control == "face" and isinstance(value, str) and len(value) <= 80:
                self.robot.face(value)
            else:
                raise ValueError("unsupported peripheral value")
        else:
            raise ValueError(f"unsupported command: {cmd}")

    async def _send(self, ws):
        while True:
            await ws.send(json.dumps(await self.outbox.get()))

    async def _receive(self, ws):
        async for raw in ws:
            if not self.hardware_ready.is_set():
                return
            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict) or not isinstance(msg.get("id"), str):
                    continue
                # Prevent duplicate, unsolicited commands from changing hardware state.
                error = None
                try:
                    await asyncio.to_thread(self.execute, msg)
                except Exception as exc:
                    error = str(exc)
                    LOG.warning("Command %s rejected: %s", msg.get("type"), error)
                await ws.send(json.dumps(ack(msg, error)))
            except (ValueError, TypeError) as exc:
                LOG.warning("Ignoring invalid server message: %s", exc)

    async def _wait_hardware_lost(self):
        while self.hardware_ready.is_set() and not self.stop_event.is_set():
            await asyncio.sleep(0.2)

    async def _mrr(self):
        while not self.stop_event.is_set():
            await self.hardware_ready.wait()
            if self.stop_event.is_set(): break
            try:
                async with websockets.connect(self.cfg.server_url, ping_interval=15,
                                              ping_timeout=8, max_size=1048576) as ws:
                    await ws.send(json.dumps(hello(
                        self.cfg.name, self.cfg.description, self.cfg.color,
                        self.cfg.max_speed, self.cfg.video)))
                    await ws.send(json.dumps(state(
                        charging=self.robot.charging, headlight=self.robot.headlight)))
                    self.publish_sensor()
                    # Never replay stale movement or telemetry from before reconnection.
                    while not self.outbox.empty():
                        self.outbox.get_nowait()
                    LOG.info("Registered rover %s with %s", self.cfg.name, self.cfg.server_url)
                    sender = asyncio.create_task(self._send(ws))
                    receiver = asyncio.create_task(self._receive(ws))
                    health = asyncio.create_task(self._wait_hardware_lost())
                    done, pending = await asyncio.wait([sender, receiver, health], return_when=asyncio.FIRST_COMPLETED)
                    for task in pending: task.cancel()
                    for task in done: task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("MRR disconnected: %s", exc)
            self.robot.stop_drive()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass

    async def _hardware(self):
        while not self.stop_event.is_set():
            try:
                await asyncio.to_thread(self.wifi.wait)
                await asyncio.to_thread(self.robot.connect)
                LOG.info("Cozmo hardware ready")
                self.hardware_ready.set()
                while not self.stop_event.is_set():
                    if not self.wifi.active() or not self.robot.is_healthy():
                        raise RuntimeError("Cozmo Wi-Fi or state stream lost")
                    self.robot.watchdog(self.cfg.watchdog_seconds)
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Cozmo hardware unavailable: %s", exc)
            finally:
                self.hardware_ready.clear()
                self.robot.stop_drive()
                await asyncio.to_thread(self.robot.close)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass

    async def run(self):
        if self.publisher: self.publisher.start()
        tasks = [asyncio.create_task(self._hardware()), asyncio.create_task(self._mrr())]
        try:
            await self.stop_event.wait()
        finally:
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.robot.close()
            if self.publisher: self.publisher.stop()

def main():
    parser = argparse.ArgumentParser(description="Standalone Cozmo bridge to MRR")
    parser.add_argument("--config", default="/etc/cozmo-roverd.toml")
    parser.add_argument("--check", action="store_true", help="Validate configuration and adapter without connecting")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load(args.config)
    if args.check:
        Wifi(cfg).verify()
        print("Configuration and dedicated Wi-Fi adapter OK")
        return
    async def start():
        bridge = Bridge(cfg)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, bridge.stop_event.set)
        await bridge.run()
    asyncio.run(start())

if __name__ == "__main__":
    main()
