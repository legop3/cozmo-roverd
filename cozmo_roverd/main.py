"""Supervise dedicated Cozmo Wi-Fi, PyCozmo, MRR WebSocket and RTSP publishing."""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
import websockets
from .config import load, rtsp_target
from .media import Publisher
from .protocol import ack, bounded_speed, finite, hello, sensor, state
from .robot import Robot
from .wifi import Wifi
from . import diagnostics

LOG = logging.getLogger("cozmo-roverd")

def _env_enabled(name, default=False):
    """Explicit opt-out for safe side LEDs, opt-in for experimental dock LEDs."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class Bridge:
    def __init__(self, config):
        self.cfg = config
        self.stop_event = asyncio.Event()
        self.hardware_ready = asyncio.Event()
        self.state_dirty = asyncio.Event()
        self.results = asyncio.Queue(maxsize=16)
        self.mrr_connected = False
        self.hardware_stage = "starting"
        self.last_hardware_error = None
        self.hardware_reconnect_attempts = 0
        self.last_state_sent = 0.0
        self.loop = asyncio.get_running_loop()
        self.wifi = Wifi(config)
        self.publisher = Publisher(rtsp_target(config), config.fps, hud=config.hud) if config.video else None
        self.robot = Robot(max_speed=config.max_speed, on_state=self.publish_state,
                           on_sensor=self.publish_sensor, on_frame=self.handle_frame,
                           camera_mode=config.camera_mode)
        self.head_degrees = 10.0
        self._diag_last_sample = 0.0
        if self.publisher:
            self.publisher.state_provider = self.hud_state

    def publish_state(self):
        # Status callbacks originate in PyCozmo's receive thread. Coalesce
        # changes rather than queuing every packet (which can starve state).
        self.loop.call_soon_threadsafe(self.state_dirty.set)

    def current_state(self):
        battery = self.robot.battery
        # Battery voltage and charger flags are measurements, not a heartbeat.
        # Cozmo can quiet its RobotState stream when it settles on the dock.
        # Keep the last measured values rather than clearing MRR's battery
        # panel after three seconds. Freshness is reported independently
        # in /api/telemetry, never hidden by replacing data with null.
        if battery.voltage is None and battery.docked is None:
            return state(charging=None, docked=None,
                         headlight=self.robot.headlight, voltage=None,
                         driving=self.robot.ready,
                         dock_assist=self.robot.dock_assist,
                         docking_phase=self.robot.docking_phase)
        return state(charging=battery.charging,
                     docked=battery.docked,
                     headlight=self.robot.headlight,
                     voltage=battery.voltage,
                     battery=battery.battery_state(),
                     driving=self.robot.ready,
                     dock_assist=self.robot.dock_assist,
                     docking_phase=self.robot.docking_phase)

    def current_sensor(self):
        latest = self.robot.latest
        if not latest:
            return None
        wheels = latest.get("wheel_speeds_mmps") or {}
        return sensor(voltage=latest.get("battery_voltage_v"),
                      left=wheels.get("left"), right=wheels.get("right"),
                      on_charger=latest.get("on_charger"),
                      cliff=latest.get("cliff_detected"),
                      cliff_raw=latest.get("raw_robot_state", {}).get("cliff_channels"))

    def publish_sensor(self):
        # Sensors are sent at a bounded 5 Hz in _send, not for every hardware
        # packet. The latest complete snapshot always wins.
        pass

    def handle_frame(self, image):
        self.robot.observe_camera_brightness(image)
        if self.publisher:
            self.publisher.submit(image)

    def report_result(self, command_id, error=None):
        def enqueue():
            if not self.results.full():
                self.results.put_nowait({
                    "type": "commandResult", "id": command_id,
                    "status": "error" if error else "ok",
                    **({"error": str(error)[:180]} if error else {}),
                })
        self.loop.call_soon_threadsafe(enqueue)

    def execute(self, msg):
        cmd = msg.get("type")
        if cmd == "drive":
            payload = msg.get("driveDirect")
            if not isinstance(payload, dict): raise ValueError("driveDirect missing")
            left = bounded_speed(payload.get("left"), self.cfg.max_speed)
            right = bounded_speed(payload.get("right"), self.cfg.max_speed)
            if self.robot.undock_active:
                self.robot.interrupt_undock()
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
        elif cmd == "undock":
            backoff = msg.get("backoff", True)
            if type(backoff) is not bool:
                raise ValueError("undock.backoff must be boolean")
            self.robot.start_undock(msg["id"], backoff, self.report_result)
        elif cmd == "dockAssist":
            self.robot.assist_docking()
        elif cmd == "cancelDocking":
            self.robot.cancel_docking()
        elif cmd == "autoDock":
            raise ValueError("Automatic charger seeking requires charger localization; dock assist is available")
        elif cmd == "motors":
            payload = msg.get("motorPwm")
            if not isinstance(payload, dict):
                raise ValueError("motorPwm missing")
            main = payload.get("main")
            if type(main) is not int or not -127 <= main <= 127:
                raise ValueError("motorPwm.main must be an integer -127..127")
            self.robot.lift_motor(main)
        elif cmd == "tts":
            payload = msg.get("tts")
            if not isinstance(payload, dict):
                raise ValueError("tts payload missing")
            text = payload.get("text", "")
            if not isinstance(text, str):
                raise ValueError("TTS text must be a string")
            # Display the most recent TTS text even when playback is disabled.
            self.robot.note_tts(text)
            if payload.get("speak", True) is not False:
                # Repeated OutputAudio attempts froze the robot and
                # interrupted its camera/control link. Preserve the text
                # on the OLED but explicitly tell MRR the hardware speaker
                # is disabled; never start a synthesis or audio worker.
                raise RuntimeError(
                    "Cozmo hardware speech disabled for robot stability; "
                    "message retained on face")
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
            elif control == "camera_auto_exposure" and type(value) is bool:
                if value:
                    self.robot.set_camera_auto_exposure()
            elif control == "camera_manual_exposure" and type(value) is int and 1 <= value <= 67:
                self.robot.set_camera_manual_exposure(value)
            else:
                raise ValueError("unsupported peripheral value")
        else:
            raise ValueError(f"unsupported command: {cmd}")

    async def _send(self, ws, initial_state=None):
        last_sensor = 0.0
        last_state = 0.0
        # Avoid emitting full state for each tiny sensor change or timer tick.
        # The receiving server also has a targeted 5 Hz sensor channel.
        previous_state = json.dumps(initial_state) if initial_state is not None else None
        while not self.stop_event.is_set():
            while not self.results.empty():
                await ws.send(json.dumps(self.results.get_nowait()))
            now = time.monotonic()
            if self.state_dirty.is_set() or now - last_state >= 1.0:
                self.state_dirty.clear()
                current = json.dumps(self.current_state())
                if current != previous_state:
                    await ws.send(current)
                    previous_state = current
                last_state = now
            if self.hardware_ready.is_set() and now - last_sensor >= 0.2:
                reading = self.current_sensor()
                if reading:
                    await ws.send(json.dumps(reading))
                last_sensor = now
            await asyncio.sleep(0.05)

    def hud_state(self):
        latest = self.robot.latest
        battery = self.robot.battery
        pose = latest.get("pose") or {}
        wheels = latest.get("wheel_speeds_mmps") or {}
        return {
            "connected": self.robot.ready,
            "mrr_connected": self.mrr_connected,
            "hardware_stage": self.hardware_stage,
            "hardware_reconnect_attempts": self.hardware_reconnect_attempts,
            "last_hardware_error": self.last_hardware_error,
            "battery_pct": battery.percent,
            "voltage": battery.voltage,
            "docked": battery.docked,
            "charging": battery.charging,
            "cliff": latest.get("cliff_detected"),
            "picked_up": latest.get("picked_up"),
            "head": latest.get("head_angle_deg"),
            "lift": latest.get("lift_height_mm"),
            "heading": pose.get("heading_deg"),
            "pitch": pose.get("pitch_deg"),
            "accel": latest.get("accelerometer"),
            "gyro": latest.get("gyroscope"),
            "wheel_command": getattr(self.robot, "commanded_wheels", (0, 0)),
            "left": wheels.get("left"), "right": wheels.get("right"),
            "sample_age": battery.age_seconds(),
        }

    async def _telemetry_file(self):
        """Export Cozmo-only fields the MRR sensor protocol cannot represent."""
        target = Path("/run/cozmo-roverd/telemetry.json")
        last_face_at = -1.0
        last_telemetry_at = -1.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            if last_face_at < 0 or now - last_face_at >= 0.25:
                try:
                    await asyncio.to_thread(self.robot.refresh_face)
                    await asyncio.to_thread(self.robot.advance_face_display)
                except Exception as exc:
                    LOG.warning("Face status refresh failed: %s", exc)
                last_face_at = now
            # Update sensor gain/shutter in the normal worker, never from the
            # camera receive thread. No digital video brightness processing.
            try:
                await asyncio.to_thread(self.robot.refresh_auto_camera_exposure)
            except Exception as exc:
                LOG.warning("Automatic camera exposure update failed: %s", exc)
            # One-time steady backpack colors; charger changes only on
            # dock-contact / dock-assist transitions. Deduped in Robot.
            # A congested send window defers updates until it clears.
            try:
                await asyncio.to_thread(self.robot.refresh_status_lights)
            except Exception as exc:
                LOG.warning("Backpack lights update failed: %s", exc)
            if last_telemetry_at < 0 or now - last_telemetry_at >= 1.0:
                try:
                    await asyncio.to_thread(self.robot.refresh_charger_lights)
                except Exception as exc:
                    LOG.warning("Charger lights update failed: %s", exc)
            if last_telemetry_at >= 0 and now - last_telemetry_at < 1.0:
                await asyncio.sleep(0.05)
                continue
            last_telemetry_at = now
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                snap = dict(self.robot.latest)
                if snap:
                    snap["battery_percent_estimated"] = self.current_state()["state"]["battery"]["percent"]
                output = {
                    "hardware_connected": self.robot.ready,
                    "hardware_stage": self.hardware_stage,
                    "last_hardware_error": self.last_hardware_error,
                    "hardware_reconnect_attempts": self.hardware_reconnect_attempts,
                    "mrr_connected": self.mrr_connected,
                    "cozmo": snap,
                    "rover_state": self.current_state()["state"],
                    "video": self.publisher.diagnostics() if self.publisher else {"enabled": False},
                    "packets_received": self.robot.packets_received,
                    "last_tts": self.robot.last_tts,
                    "last_tts_error": self.robot.last_tts_error,
                    "last_tts_frames": self.robot.last_tts_frames,
                    "last_tts_played_at": self.robot.last_tts_played_at,
                    "face_frames_sent": self.robot.face_frames_sent,
                    "face_last_encoded_bytes": self.robot.face_last_encoded_bytes,
                    "face_encoded_on_pixels": self.robot.face_encoded_on_pixels,
                    "face_decoded_columns": self.robot.face_decoded_columns,
                    "face_last_error": self.robot.face_last_error,
                    "speaker": self.robot.speaker_diagnostics(),
                    "transport": self.robot.transport_diagnostics(),
                    "last_packet_error": self.robot.last_packet_error,
                    "battery_tracker": self.robot.battery.diagnostic(),
                    "max_speed_config_mmps": self.cfg.max_speed,
                    "commanded_wheels_mmps": {"left": self.robot.commanded_wheels[0],
                                              "right": self.robot.commanded_wheels[1]},
                    "camera_exposure": {"mode": self.robot.camera_mode, "manual_ms": self.robot.manual_exposure_ms,
                                        "auto_gain": self.robot._auto_gain, "auto_exposure_ms": self.robot._auto_exposure_ms,
                                        "measured_brightness": self.robot._auto_measured_brightness},
                    "camera_fov": {"horizontal_deg": self.robot.latest.get("camera", {}).get("fov_x_deg"),
                                   "vertical_deg": self.robot.latest.get("camera", {}).get("fov_y_deg")},
                    "telemetry_stale": self.robot.battery.stale(),
                    "telemetry_age_seconds": self.robot.battery.age_seconds(),
                }
                tmp = target.with_suffix(".tmp")
                tmp.write_text(json.dumps(output))
                os.replace(tmp, target)
            except (OSError, TypeError, ValueError) as exc:
                LOG.warning("Cannot export local telemetry: %s", exc)
            await asyncio.sleep(0.05)

    async def _receive(self, ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict) or not isinstance(msg.get("id"), str):
                    continue
                rec = diagnostics.active()
                if rec:
                    rec.command(msg)
                # Prevent duplicate, unsolicited commands from changing hardware state.
                error = None
                try:
                    if not self.hardware_ready.is_set():
                        raise RuntimeError("Cozmo temporarily disconnected; reconnecting")
                    await asyncio.to_thread(self.execute, msg)
                except Exception as exc:
                    error = str(exc)
                    # MRR periodically probes optional stream/audio-level
                    # controls this robot does not implement. Keep the error
                    # ACK, but don't obscure actual hardware failures.
                    if msg.get("type") in ("audioLevels", "sensorStream"):
                        LOG.debug("Optional MRR command %s unsupported: %s",
                                  msg.get("type"), error)
                    else:
                        LOG.warning("Command %s rejected: %s", msg.get("type"), error)
                    if rec:
                        rec.event("command_rejected", command_type=str(msg.get("type"))[:50],
                                  error=str(error)[:160])
                await ws.send(json.dumps(ack(msg, error)))
            except (ValueError, TypeError) as exc:
                LOG.warning("Ignoring invalid server message: %s", exc)

    async def _mrr(self):
        while not self.stop_event.is_set():
            try:
                async with websockets.connect(self.cfg.server_url, ping_interval=15,
                                              ping_timeout=8, max_size=1048576) as ws:
                    await ws.send(json.dumps(hello(
                        self.cfg.name, self.cfg.description, self.cfg.color,
                        self.cfg.max_speed, self.cfg.video, self.cfg.private)))
                    initial_state = self.current_state()
                    await ws.send(json.dumps(initial_state))
                    self.mrr_connected = True
                    if diagnostics.active():
                        diagnostics.active().event("mrr_connected")
                    self.state_dirty.clear()
                    while not self.results.empty():
                        self.results.get_nowait()
                    LOG.info("Registered rover %s with %s", self.cfg.name, self.cfg.server_url)
                    sender = asyncio.create_task(self._send(ws, initial_state))
                    receiver = asyncio.create_task(self._receive(ws))
                    done, pending = await asyncio.wait([sender, receiver], return_when=asyncio.FIRST_COMPLETED)
                    for task in pending: task.cancel()
                    for task in done: task.result()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("MRR disconnected: %s", exc)
            self.mrr_connected = False
            if diagnostics.active():
                diagnostics.active().event("mrr_disconnected")
            # Losing the MRR connection does not change the last commanded
            # wheel speed. Only a subsequent drive command or an explicit
            # robot lifecycle operation should do that.
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass

    async def _hardware(self):
        while not self.stop_event.is_set():
            try:
                self.hardware_stage = "connecting_wifi"
                if diagnostics.active():
                    diagnostics.active().event("hardware_stage", stage=self.hardware_stage)
                await asyncio.to_thread(self.wifi.wait)
                self.hardware_stage = "connecting_robot"
                if diagnostics.active():
                    diagnostics.active().event("hardware_stage", stage=self.hardware_stage)
                try:
                    await asyncio.wait_for(asyncio.to_thread(self.robot.connect),
                                           timeout=35.0)
                except asyncio.TimeoutError:
                    # A blocked PyCozmo connect/stop leaves its UDP thread
                    # alive. Only process isolation guarantees a clean new
                    # socket; systemd automatically restarts this service.
                    LOG.critical("PyCozmo connect stuck for 35s; restarting service")
                    if diagnostics.active():
                        diagnostics.active().incident(
                            reason="connect_timeout_35s", stage=self.hardware_stage,
                            reconnect_attempts=self.hardware_reconnect_attempts)
                    os._exit(1)
                self.hardware_stage = "online"
                self.last_hardware_error = None
                LOG.info("Cozmo hardware ready")
                if diagnostics.active():
                    diagnostics.active().event("hardware_ready",
                        reconnect_attempts=self.hardware_reconnect_attempts)
                self.hardware_ready.set()
                # State updates can pause during charging, exposure changes,
                # camera traffic, or transient radio congestion. Do not tear
                # down an otherwise-associated robot after five seconds.
                # NetworkManager is queried only every 2 seconds; require
                # repeated loss before reactivating the Wi-Fi profile.
                wifi_misses = 0
                last_wifi_check = 0.0
                stalled_ack_since = None
                previous_expected_seq = None
                while not self.stop_event.is_set():
                    now = time.monotonic()
                    if now - last_wifi_check >= 2.0:
                        if self.wifi.active():
                            wifi_misses = 0
                        else:
                            wifi_misses += 1
                            if wifi_misses >= 3:
                                raise RuntimeError("Cozmo Wi-Fi association lost (3 checks)")
                        last_wifi_check = now
                    # A full reliable send window can stall wheel commands
                    # while the camera continues arriving. Watch for a real
                    # lack of ACK progress, rather than treating every brief
                    # packet/telemetry pause as a disconnection.
                    transport = (self.robot.transport_diagnostics()
                                 if hasattr(self.robot, "transport_diagnostics") else None)
                    rec = diagnostics.active()
                    if rec and now - self._diag_last_sample >= 1.0:
                        frame_at = self.publisher.last_frame_time if self.publisher else None
                        rec.sample(
                            stage=self.hardware_stage, transport=transport,
                            packet_count=self.robot.packets_received,
                            last_sensor_age=(round(now - self.robot.last_sensor, 2)
                                             if self.robot.last_sensor else None),
                            camera_age=(round(now - frame_at, 2) if frame_at else None),
                            battery_voltage=self.robot.battery.voltage)
                        self._diag_last_sample = now
                    if transport:
                        expected = transport["next_ack_expected"]
                        pending = transport["pending_reliable_packets"]
                        if pending == 0 or expected != previous_expected_seq:
                            stalled_ack_since = None
                        elif stalled_ack_since is None:
                            stalled_ack_since = now
                        elif now - stalled_ack_since >= 12.0:
                            # Hardware speech is permanently disabled.
                            # A reliable-ACK stall is a transport fault, not
                            # evidence that the last TTS message caused it.
                            raise RuntimeError(
                                f"Cozmo reliable command channel stalled: "
                                f"{pending} packets pending without ACK progress")
                        previous_expected_seq = expected
                    # A prolonged silence indicates a stuck PyCozmo session,
                    # not a brief telemetry pause. Keep the Wi-Fi association
                    # and reconnect the client only after this grace period.
                    if self.robot.last_sensor and not self.robot.battery.docked and (
                        now - self.robot.last_sensor > 45.0):
                        raise RuntimeError("No Cozmo RobotState for 45 seconds; restarting PyCozmo only")
                    await asyncio.sleep(0.25)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_hardware_error = str(exc)[:240]
                LOG.warning("Cozmo hardware unavailable (%s): %s", self.hardware_stage, exc)
                if diagnostics.active():
                    now = time.monotonic()
                    diagnostics.active().incident(
                        reason=str(exc)[:240], stage=self.hardware_stage,
                        reconnect_attempts=self.hardware_reconnect_attempts,
                        last_sensor_age=(round(now - self.robot.last_sensor, 2)
                                         if self.robot.last_sensor else None),
                        transport=self.robot.transport_diagnostics(),
                        video=(self.publisher.diagnostics() if self.publisher else None))
            finally:
                self.hardware_ready.clear()
                self.hardware_stage = "cleaning_up"
                if diagnostics.active():
                    diagnostics.active().event("hardware_stage", stage=self.hardware_stage)
                # Tear down local PyCozmo without changing MRR registration.
                try:
                    await asyncio.wait_for(asyncio.to_thread(self.robot.close), timeout=8.0)
                except asyncio.TimeoutError:
                    # Do not spawn another client over one that never stopped.
                    # Recover by letting systemd restart the bridge process;
                    # the dead PyCozmo worker cannot be safely reused.
                    LOG.critical("PyCozmo cleanup hung for 8s; restarting bridge process")
                    if diagnostics.active():
                        diagnostics.active().incident(
                            reason="cleanup_timeout_8s", stage=self.hardware_stage,
                            reconnect_attempts=self.hardware_reconnect_attempts)
                    os._exit(1)
                except Exception:
                    LOG.exception("PyCozmo cleanup failed")
                self.hardware_reconnect_attempts += 1
                self.hardware_stage = "retrying"
                if diagnostics.active():
                    diagnostics.active().event("hardware_stage", stage=self.hardware_stage,
                                               reconnect_attempts=self.hardware_reconnect_attempts)
                self.publish_state()
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=3)
            except asyncio.TimeoutError:
                pass

    async def run(self):
        if self.publisher: self.publisher.start()
        tasks = [asyncio.create_task(self._hardware()), asyncio.create_task(self._mrr()),
                 asyncio.create_task(self._telemetry_file())]
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
    diagnostics.initialize()
    try:
        async def start():
            bridge = Bridge(cfg)
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, bridge.stop_event.set)
            await bridge.run()
        asyncio.run(start())
    finally:
        diagnostics.shutdown()

if __name__ == "__main__":
    main()
