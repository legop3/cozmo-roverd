"""Supervise dedicated Cozmo Wi-Fi, PyCozmo, MRR WebSocket and RTSP publishing."""
from __future__ import annotations
import argparse
import asyncio
import json
import logging
import math
import os
import signal
import threading
import time
from pathlib import Path
import websockets
from .config import load, rtsp_target
from .media import Publisher
from .protocol import ack, bounded_speed, finite, hello, sensor, state
from .robot import Robot
from .wifi import Wifi
from .home_assistant import DockPower, DockPowerError
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
        # MRR commands run in worker threads. Only the hardware supervisor
        # may destroy/recreate the active PyCozmo client.
        self._reconnect_requested = threading.Event()
        self.state_dirty = asyncio.Event()
        self.results = asyncio.Queue(maxsize=16)
        self.mrr_connected = False
        self.hardware_stage = "starting"
        self.last_hardware_error = None
        self.hardware_reconnect_attempts = 0
        # MRR remains registered for 10 uninterrupted minutes of hardware
        # outage, then goes offline until Cozmo actually reconnects.
        self._hardware_offline_since = time.monotonic()
        self.last_state_sent = 0.0
        self.loop = asyncio.get_running_loop()
        self.wifi = Wifi(config)
        self.dock_power = (DockPower(config.home_assistant_url,
                                     config.home_assistant_token,
                                     config.home_assistant_switch)
                           if config.channel_reroll_enabled else None)
        self.channel_actual = None
        self.channel_status = "disabled" if self.dock_power is None else "waiting_for_wifi"
        self.channel_attempts = 0
        self.channel_cycles = 0
        self.channel_next_attempt_at = 0.0
        self.channel_last_error = None
        self._reroll_active = False
        self.publisher = Publisher(rtsp_target(config), config.fps, hud=config.hud) if config.video else None
        self.robot = Robot(max_speed=config.max_speed, on_state=self.publish_state,
                           on_sensor=self.publish_sensor, on_frame=self.handle_frame,
                           camera_mode=config.camera_mode, camera_enabled=config.camera_enabled)
        # Reuse recent SOC/sag learning across service restarts. Do not write
        # persistent files from the PyCozmo receive callback.
        self._gauge_file = Path("/var/log/cozmo-roverd/battery-gauge.json")
        try:
            if self._gauge_file.is_file():
                saved = json.loads(self._gauge_file.read_text())
                self.robot.battery.gauge.restore(saved)
                self.robot.battery.restore_charge_state(saved)
                # Recent raw voltage is valuable even if Cozmo goes silent
                # while charging, including when the bridge restarts.
                self.robot.battery.restore_voltage(saved)
        except (OSError, ValueError, TypeError) as exc:
            LOG.warning("Ignoring stored fuel gauge data: %s", exc)
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
                         driving=self.hardware_ready.is_set(),
                         dock_assist=self.robot.dock_assist,
                         docking_phase=self.robot.docking_phase)
        return state(charging=battery.charging,
                     docked=battery.docked,
                     headlight=self.robot.headlight,
                     voltage=battery.voltage,
                     battery=battery.battery_state(),
                     driving=self.hardware_ready.is_set(),
                     dock_assist=self.robot.dock_assist,
                     docking_phase=self.robot.docking_phase)

    def current_sensor(self):
        latest = self.robot.latest
        battery = self.robot.battery
        if not latest and battery.voltage is None:
            return None
        wheels = latest.get("wheel_speeds_mmps") or {}
        # The last measured voltage is retained independently of RobotState.
        # Docked robots can suspend telemetry or report an invalid voltage;
        # never overwrite a valid sensor reading with an empty snapshot.
        return sensor(voltage=battery.voltage,
                      left=wheels.get("left"), right=wheels.get("right"),
                      on_charger=battery.docked,
                      cliff=latest.get("cliff_detected"),
                      cliff_raw=latest.get("raw_robot_state", {}).get("cliff_channels"))

    def publish_sensor(self):
        # Sensors are sent at a bounded 5 Hz in _send, not for every hardware
        # packet. The latest complete snapshot always wins.
        pass

    def handle_frame(self, image):
        # Never leak preflight frames into RTSP or run brightness analysis
        # until the hardware supervisor has promoted the verified connection.
        if not self.hardware_ready.is_set():
            return
        if self.publisher:
            self.publisher.submit(image)
        self.robot.queue_camera_analysis(image)

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
            elif control == "reconnect" and value is True:
                # Request a clean teardown through _hardware. Do not close
                # the client here: execute() runs on a different thread.
                self._reconnect_requested.set()
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

    MRR_HARDWARE_GRACE_SECONDS = 10 * 60

    def _mrr_offline_remaining(self, now=None):
        """Seconds left before hardware outage removes this rover from MRR."""
        ready = getattr(self, "hardware_ready", None)
        if ready is not None and ready.is_set():
            return None
        since = getattr(self, "_hardware_offline_since", None)
        if since is None:
            return None
        now = time.monotonic() if now is None else now
        return max(0, int(math.ceil(self.MRR_HARDWARE_GRACE_SECONDS - (now - since))))

    async def _watch_mrr_hardware(self):
        """End MRR registration only if an outage exceeds the grace period."""
        while not self.stop_event.is_set():
            if self._mrr_offline_remaining() == 0:
                LOG.warning("Cozmo unavailable for 10 minutes; unregistering from MRR")
                rec = diagnostics.active()
                if rec:
                    rec.event("mrr_hardware_grace_expired")
                return
            await asyncio.sleep(0.5)

    def hud_state(self):
        latest = self.robot.latest
        battery = self.robot.battery
        pose = latest.get("pose") or {}
        wheels = latest.get("wheel_speeds_mmps") or {}
        return {
            "connected": self.hardware_ready.is_set(),
            "mrr_offline_remaining_seconds": self._mrr_offline_remaining(),
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
            "channel_reroll": self.channel_state(),
        }

    async def _maintenance(self, method, interval):
        """Run each maintenance responsibility independently of the others.

        Slow camera settings or charger discovery cannot hold up telemetry
        export, the next maintenance task, or the PyCozmo receive thread.
        Robot command serialization still belongs to the Robot's lock.
        """
        while not self.stop_event.is_set():
            try:
                if self.dock_power and not self.hardware_ready.is_set():
                    # Channel inspection is for reading charger contact only.
                    # No LED, exposure, lift, idle-wake or face commands.
                    await asyncio.sleep(min(interval, 0.5))
                    continue
                await asyncio.to_thread(method)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Cozmo maintenance failed: %s", method.__name__)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _telemetry_file(self):
        """Export and persist at most once a second, independently of motors."""
        target = Path("/run/cozmo-roverd/telemetry.json")
        last_gauge_saved_at = -1.0
        while not self.stop_event.is_set():
            now = time.monotonic()
            if last_gauge_saved_at < 0 or now - last_gauge_saved_at >= 30.0:
                # Save only a compact, validated model snapshot, at most once
                # per 30 seconds and never for each incoming battery packet.
                last_gauge_saved_at = now
                if hasattr(self, "_gauge_file"):
                    try:
                        learned = self.robot.battery.gauge.export()
                        if learned is not None:
                            learned.update(self.robot.battery.voltage_snapshot())
                            learned.update(self.robot.battery.charge_state_snapshot())
                            self._gauge_file.parent.mkdir(parents=True, exist_ok=True)
                            gauge_tmp = self._gauge_file.with_suffix(".tmp")
                            gauge_tmp.write_text(json.dumps(learned))
                            os.replace(gauge_tmp, self._gauge_file)
                    except (OSError, ValueError, TypeError) as exc:
                        LOG.warning("Cannot save battery gauge calibration: %s", exc)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                snap = dict(self.robot.latest)
                # Latest rich RobotState may be absent after reconnect; battery
                # retains the last measured voltage independently.
                snap["battery_voltage_v"] = self.robot.battery.voltage
                snap["battery_percent_estimated"] = self.robot.battery.percent
                snap["on_charger"] = self.robot.battery.docked
                snap["charging"] = self.robot.battery.charging
                output = {
                    "generated_at_ms": int(time.time() * 1000),
                    "hardware_connected": self.hardware_ready.is_set(),
                    "hardware_stage": self.hardware_stage,
                    "last_hardware_error": self.last_hardware_error,
                    "hardware_reconnect_attempts": self.hardware_reconnect_attempts,
                    "channel_reroll": self.channel_state(),
                    "mrr_connected": self.mrr_connected,
                    "cozmo": snap,
                    "rover_state": self.current_state()["state"],
                    "video": self.publisher.diagnostics() if self.publisher else {"enabled": False},
                    "packets_received": self.robot.packets_received,
                    "state_packets_coalesced": self.robot.state_packets_coalesced,
                    "camera_analysis_skipped": self.robot.camera_analysis_skipped,
                    "diagnostic_minimal": self.cfg.diagnostic_minimal,
                    "camera_transport": {
                        "enabled": self.robot.camera_enabled,
                        "image_chunks_received": self.robot.camera_chunks_received,
                        "complete_images_received": self.robot.camera_images_completed,
                        "last_image_chunk_age_seconds": (
                            round(max(0.0, time.monotonic() - self.robot.camera_last_chunk_at), 2)
                            if self.robot.camera_last_chunk_at is not None else None),
                        "last_complete_image_age_seconds": (
                            round(max(0.0, time.monotonic() - self.robot.camera_last_image_at), 2)
                            if self.robot.camera_last_image_at is not None else None),
                    },
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
            # File export is diagnostic-only and must not occupy the main
            # asyncio loop 20 times a second or compete with live commands.
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

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
            # Once the grace period expires, do not repeatedly re-register
            # an offline rover. Hardware retries continue independently.
            if self._mrr_offline_remaining() == 0:
                await self.hardware_ready.wait()
                if self.stop_event.is_set():
                    break
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
                    availability = asyncio.create_task(self._watch_mrr_hardware())
                    tasks = (sender, receiver, availability)
                    try:
                        done, pending = await asyncio.wait(
                            tasks, return_when=asyncio.FIRST_COMPLETED)
                        for task in done:
                            task.result()
                    finally:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
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

    def _camera_link_active(self, now):
        """A recent live PyCozmo frame is positive evidence of an active link."""
        publisher = self.publisher
        return (publisher is not None
                and publisher.last_frame_time is not None
                and now - publisher.last_frame_time < 5.0)

    def channel_state(self):
        return {
            "enabled": self.dock_power is not None,
            "target": self.cfg.channel_target if self.dock_power else None,
            "actual": self.channel_actual,
            "status": self.channel_status,
            "attempt": self.channel_attempts,
            "retry_in_seconds": (max(0, int(self.channel_next_attempt_at -
                                            time.monotonic()))
                                 if self.channel_next_attempt_at > time.monotonic() else 0),
            "error": self.channel_last_error,
        }

    def _channel_stage(self, status):
        self.channel_status = status
        self.hardware_stage = "channel_" + status
        self.publish_state()
        if diagnostics.active():
            diagnostics.active().event("channel_reroll", status=status,
                                       actual=self.channel_actual,
                                       target=self.cfg.channel_target,
                                       attempt=self.channel_attempts)

    def _docked_for_reroll(self):
        return (self.robot.ready and self.robot._dock_confirmed_session is True
                and self.robot.on_charger is True
                and self.robot.battery.docked is True
                and not self.robot.moving and not self.robot.undock_active
                and not self.robot.dock_assist
                and not any(self.robot.commanded_wheels))

    async def _restart_docked_for_channel(self):
        """One dock-verified shutdown/wake cycle, never normal robot startup."""
        self.channel_attempts += 1
        self._reroll_active = True
        self._channel_stage("shutting_down")
        shutdown_queued = False
        cycle_started = False
        try:
            await asyncio.to_thread(self.robot.shutdown_for_channel_reroll)
            shutdown_queued = True
            # Allow the reliable sender to transmit the shutdown command.
            await asyncio.sleep(2)
            self._channel_stage("cycling_dock_power")
            cycle_started = True
            # DockPower.cycle restores ON in its own finally, including
            # when Home Assistant rejects or times out during turn_off.
            await asyncio.shield(asyncio.to_thread(self.dock_power.cycle))
        finally:
            try:
                # Never leave a robot shut down if interrupted between
                # the command and the power cycle.
                if shutdown_queued and not cycle_started:
                    self._channel_stage("cycling_dock_power")
                    try:
                        await asyncio.shield(asyncio.to_thread(self.dock_power.cycle))
                    except Exception:
                        LOG.critical("Unable to wake Cozmo after shutdown; check dock switch")
            finally:
                self._reroll_active = False

    async def _prepare_channel(self):
        """Keep the robot in telemetry-only inspection until target channel.

        The RTSP publisher and MRR client run separately throughout this
        method. hardware_ready remains clear: no commands, sensors, camera,
        lights or maintenance can run until normal promotion succeeds.
        """
        if not self.dock_power:
            return
        while not self.stop_event.is_set():
            self._channel_stage("inspecting_dock")
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self.robot.connect, inspection=True),
                    timeout=35)
            except asyncio.TimeoutError:
                LOG.critical("Cozmo inspection connect stuck; restarting service")
                os._exit(1)

            verified = False
            try:
                while not self.stop_event.is_set():
                    if not await asyncio.to_thread(self.wifi.active):
                        raise RuntimeError("Cozmo Wi-Fi disconnected during channel selection")
                    self.channel_actual = await asyncio.to_thread(self.wifi.channel)
                    if self.channel_actual == self.cfg.channel_target:
                        self.channel_status = "matched"
                        self.channel_next_attempt_at = 0
                        self.channel_last_error = None
                        self._channel_stage("verified")
                        self.channel_status = "matched"
                        verified = True
                        return  # Keep this inspection socket for promotion.

                    if self.channel_actual is None:
                        self._channel_stage("waiting_for_wifi")
                    elif not self._docked_for_reroll():
                        self._channel_stage("waiting_for_dock")
                    elif time.monotonic() < self.channel_next_attempt_at:
                        self._channel_stage("retry_cooldown")
                    else:
                        try:
                            if await asyncio.to_thread(self.dock_power.state) != "on":
                                raise DockPowerError("Home Assistant dock switch is off")
                        except DockPowerError as exc:
                            self.channel_last_error = str(exc)
                            self.channel_next_attempt_at = time.monotonic() + 60
                            self._channel_stage("home_assistant_unavailable")
                        else:
                            await self._restart_docked_for_channel()
                            self._channel_stage("waiting_for_wifi")
                            break  # Close inspection socket and wait for new AP.
                    await asyncio.sleep(1)
            finally:
                if not verified:
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(self.robot.close), timeout=8)
                    except asyncio.TimeoutError:
                        LOG.critical("Cozmo inspection cleanup stuck; restarting service")
                        os._exit(1)
            if self.stop_event.is_set():
                return
            # Wi-Fi association must complete again before any new inspection.
            await asyncio.to_thread(self.wifi.wait)
        raise asyncio.CancelledError()

    async def _hardware(self):
        while not self.stop_event.is_set():
            try:
                self.hardware_stage = "connecting_wifi"
                if diagnostics.active():
                    diagnostics.active().event("hardware_stage", stage=self.hardware_stage)
                await asyncio.to_thread(self.wifi.wait)
                if self.dock_power:
                    self.hardware_stage = "channel_inspecting_dock"
                else:
                    self.hardware_stage = "connecting_robot"
                    if diagnostics.active():
                        diagnostics.active().event("hardware_stage", stage=self.hardware_stage)
                    try:
                        await asyncio.wait_for(asyncio.to_thread(self.robot.connect),
                                               timeout=35.0)
                    except asyncio.TimeoutError:
                        LOG.critical("PyCozmo connect stuck for 35s; restarting service")
                        if diagnostics.active():
                            diagnostics.active().incident(
                                reason="connect_timeout_35s", stage=self.hardware_stage,
                                reconnect_attempts=self.hardware_reconnect_attempts)
                        os._exit(1)
                if self.dock_power:
                    # The initial PyCozmo connection is telemetry-only.
                    # No camera, lights, MRR controls or maintenance until
                    # the selected Wi-Fi channel has been verified.
                    await self._prepare_channel()
                    if self.stop_event.is_set():
                        break
                    await asyncio.to_thread(self.robot.promote_inspection)
                self.hardware_stage = "online"
                self.channel_status = "matched" if self.dock_power else "disabled"
                self.last_hardware_error = None
                LOG.info("Cozmo hardware ready")
                if diagnostics.active():
                    diagnostics.active().event("hardware_ready",
                        reconnect_attempts=self.hardware_reconnect_attempts)
                self._hardware_offline_since = None
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
                    if self._reconnect_requested.is_set():
                        LOG.info("Manual Cozmo reconnection requested from MRR")
                        if diagnostics.active():
                            diagnostics.active().event("hardware_manual_reconnect")
                        break
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
                    # A live camera stream proves PyCozmo is still receiving
                    # traffic. Some firmware/charger states pause RobotState
                    # while camera frames continue; do not disconnect a
                    # working camera/control link solely for that silence.
                    camera_recent = self._camera_link_active(now)
                    if (self.robot.last_sensor and not self.robot.battery.docked
                            and not camera_recent
                            and now - self.robot.last_sensor > 45.0):
                        raise RuntimeError(
                            "No Cozmo RobotState for 45 seconds and no recent camera frames; "
                            "restarting PyCozmo only")
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
                # A momentary UI button must trigger exactly one cycle, not
                # repeat across reconnection attempts.
                self._reconnect_requested.clear()
                if getattr(self, "_hardware_offline_since", None) is None:
                    self._hardware_offline_since = time.monotonic()
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
        # Maintain independent cadences rather than one serial 50ms loop.
        if not self.cfg.diagnostic_minimal:
            tasks += [
            asyncio.create_task(self._maintenance(self.robot.refresh_auto_camera_exposure, 0.2)),
            asyncio.create_task(self._maintenance(self.robot.refresh_dock_lift, 0.25)),
            asyncio.create_task(self._maintenance(self.robot.refresh_idle_wake, 1.0)),
            asyncio.create_task(self._maintenance(self.robot.refresh_status_lights, 0.5)),
            asyncio.create_task(self._maintenance(self.robot.refresh_charger_lights, 1.0)),
            ]
        # Static face status is enabled normally; no repeating audio clock.
        if self.robot._face_transport_enabled:
            tasks += [
                asyncio.create_task(self._maintenance(self.robot.refresh_face, 0.25)),
            ]
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
