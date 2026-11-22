"""Opt-in *physical* Cozmo fault-isolation probes using unmodified PyCozmo.

Each case runs in a separate, killable process, never as a bridge command.
Stop the production bridge first. Tests send real hardware commands and can
still cause a physical Cozmo firmware hang; no unit test can establish that
these commands work on a particular robot.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import wave

from .config import load
from .diagnostics import Recorder
from .wifi import Wifi

CASES = (
    "baseline-idle", "baseline-camera",
    "backpack-stock", "backpack-side", "backpack-side-camera",
    "charger", "audio-idle", "audio-camera",
)


def _snapshot(cli):
    sender = cli.conn.send_thread
    with sender.lock:
        window = sender.window
        pending = (window.next_seq - window.expected_seq) % window.max_seq
        expected = window.expected_seq
    receiver = cli.conn.recv_thread
    return {
        "pending_reliable_packets": pending,
        "next_ack_expected": expected,
        "queued_app_commands": sender.queue.qsize(),
        "outgoing_packets": sender.outgoing_packets,
        "sent_packets_including_retries": sender.sent_packets,
        "sent_frames": sender.sent_frames,
        "discarded_frames": sender.discarded_frames,
        "received_frames": receiver.received_frames,
        "received_packets": receiver.received_packets,
        "audio_frames_reported_played": cli.num_audio_frames_played,
        "last_image_timestamp": cli.last_image_timestamp,
        "robot_status": cli.robot_status,
    }


def _tone_wav(path):
    # Short quiet, original 440 Hz test tone, no TTS/FFmpeg dependency.
    rate = 22050
    duration = 0.28
    frames = int(rate * duration)
    samples = b"".join(struct.pack("<h", int(
        2400 * math.sin(2 * math.pi * 440 * n / rate)))
        for n in range(frames))
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(samples)


def _apply_case(cli, case, recorder, test_dir):
    import pycozmo

    if case.startswith("baseline"):
        return "no_command"
    if case == "backpack-stock":
        cli.set_all_backpack_lights(pycozmo.lights.red_light)
        return "stock_set_all_backpack_lights(red)"
    if case.startswith("backpack-side"):
        cli.conn.send(pycozmo.protocol_encoder.LightStateSide(
            states=(pycozmo.lights.red_light, pycozmo.lights.off_light)))
        return "LightStateSide(red, off)"
    if case == "charger":
        p = pycozmo.protocol_encoder
        candidates = [
            factory for factory, obj in list(cli.available_objects.items())
            if getattr(obj, "object_type", None) == p.ObjectType.Charger_Basic
        ]
        if not candidates:
            return "SKIPPED: no charger advertised by Cozmo"
        cli.conn.send(p.ObjectConnect(factory_id=candidates[0], connect=True))
        recorder.event("charger_connect_requested")
        deadline = time.monotonic() + 6
        while time.monotonic() < deadline:
            matches = [
                objid for objid, data in list(cli.connected_objects.items())
                if data.get("object_type") == p.ObjectType.Charger_Basic
            ]
            if matches:
                cli.conn.send(p.CubeId(object_id=matches[0]))
                cli.conn.send(p.CubeLights(states=(
                    pycozmo.lights.red_light, pycozmo.lights.red_light,
                    pycozmo.lights.red_light, pycozmo.lights.off_light)))
                return "charger_ObjectConnect+CubeId+CubeLights"
            time.sleep(0.1)
        return "SKIPPED: charger connection not established"
    if case.startswith("audio"):
        path = test_dir / "test-tone.wav"
        _tone_wav(path)
        cli.set_volume(12000)
        cli.play_audio(str(path))
        return "stock_set_volume+play_audio(0.28-second-tone)"
    raise ValueError(case)


def _worker(config_path, case, duration, output_dir):
    import pycozmo
    from .wifi import Wifi
    test_dir = Path(output_dir)
    test_dir.mkdir(parents=True, exist_ok=True)
    report = test_dir / "measurements.jsonl"
    recorder = Recorder(test_dir)
    cli = None

    def write(event, **fields):
        with report.open("a", encoding="utf-8") as fd:
            fd.write(json.dumps({
                "utc": datetime.now(timezone.utc).isoformat(),
                "event": event, "case": case, **fields}, default=str) + "\n")
            fd.flush()
            os.fsync(fd.fileno())

    write("test_started", pycozmo_module=pycozmo.__file__)
    try:
        if subprocess.run(["systemctl", "is-active", "--quiet", "cozmo-roverd.service"]).returncode == 0:
            raise RuntimeError("Production cozmo-roverd.service is running; stop it before testing")
        cfg = load(config_path)
        write("connecting_wifi", interface=cfg.interface)
        Wifi(cfg).wait()
        animations = case.startswith("audio") or "animation" in case
        cli = pycozmo.Client(
            enable_animations=animations, enable_procedural_face=False)
        cli.start()
        cli.connect()
        cli.wait_for_robot(timeout=12)
        # Enable video only in explicit camera conditions, so failed tests
        # isolate the influence of concurrent camera streaming.
        if case.endswith("-camera"):
            cli.enable_camera(enable=True, color=True)
        write("robot_ready", animations=animations,
              camera_enabled=case.endswith("-camera"))
        # Observe a short quiet baseline *before* the potentially bad packet.
        before = _snapshot(cli)
        write("pre_command_transport", transport=before)
        time.sleep(2)
        write("pre_command_stable", transport=_snapshot(cli))
        write("command_about_to_send", requested_case=case)
        action = _apply_case(cli, case, recorder, test_dir)
        write("command_issued", action=action, transport=_snapshot(cli))
        if action.startswith("SKIPPED"):
            return 2
        last_ack = None
        stalled_at = None
        end = time.monotonic() + duration
        while time.monotonic() < end:
            metrics = _snapshot(cli)
            write("sample", transport=metrics)
            recorder.sample(stage=case, transport=metrics)
            if metrics["pending_reliable_packets"] > 0:
                if metrics["next_ack_expected"] != last_ack:
                    stalled_at = time.monotonic()
                elif stalled_at is not None and time.monotonic() - stalled_at > 4.0:
                    write("stalled", transport=metrics)
                    recorder.incident(reason="ACK_no_progress_4s", stage=case,
                                      transport=metrics)
                    return 3
            else:
                stalled_at = None
            last_ack = metrics["next_ack_expected"]
            time.sleep(0.25)
        final = _snapshot(cli)
        played_delta = (final["audio_frames_reported_played"]
                        - before["audio_frames_reported_played"])
        if case.startswith("audio") and played_delta <= 0:
            write("audio_not_reported_played", transport=final,
                  frames_reported_during_test=played_delta)
            recorder.incident(reason="audio_frames_not_reported_played",
                              stage=case, transport=final)
            return 4
        if final["pending_reliable_packets"] >= 6:
            write("outstanding_commands_at_end", transport=final)
            recorder.incident(reason="commands_still_pending_at_end",
                              stage=case, transport=final)
            return 5
        write("test_finished", transport=final,
              audio_frames_reported_during_test=played_delta)
        return 0
    except Exception as exc:
        write("test_exception", exception_type=type(exc).__name__, message=str(exc))
        recorder.incident(reason=str(exc), stage=case)
        return 1
    finally:
        if cli is not None:
            write("cleanup_start")
            try:
                cli.stop()
                write("cleanup_done")
            except Exception as exc:
                write("cleanup_error", message=str(exc))
        recorder.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--config", default="/etc/cozmo-roverd.toml")
    parser.add_argument("--duration", type=int, default=15)
    parser.add_argument("--output-dir", default="/var/log/cozmo-roverd/hardware-tests")
    parser.add_argument("--i-understand-risk", action="store_true",
                        help="Explicit opt-in: actual LED/audio packets may hang the robot")
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 3 <= args.duration <= 120:
        parser.error("--duration must be 3..120 seconds")
    if not args._worker and not args.i_understand_risk:
        parser.error("Requires --i-understand-risk; these tests send real robot commands")
    if not args._worker and subprocess.run(
            ["systemctl", "is-active", "--quiet", "cozmo-roverd.service"]).returncode == 0:
        parser.error("Stop cozmo-roverd.service before running isolated hardware tests")
    if args._worker:
        return _worker(args.config, args.case, args.duration, args.output_dir)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = Path(args.output_dir) / (run_id + "-" + args.case)
    directory.mkdir(parents=True, exist_ok=False)
    command = [
        sys.executable, "-m", "cozmo_roverd.hardware_tests",
        "--_worker", "--case", args.case, "--config", args.config,
        "--duration", str(args.duration), "--output-dir", str(directory),
    ]
    print("Running isolated", args.case, "test; output:", directory, flush=True)
    try:
        # Even a blocked PyCozmo stop() cannot hang the parent/test suite.
        proc = subprocess.run(command, timeout=args.duration + 90)
        return proc.returncode
    except subprocess.TimeoutExpired:
        with (directory / "measurements.jsonl").open("a") as fd:
            fd.write(json.dumps({"event": "parent_killed_hung_worker",
                                 "case": args.case}) + "\n")
        print("Hardware-test worker timed out and was killed; inspect:", directory,
              file=sys.stderr)
        return 124


if __name__ == "__main__":
    sys.exit(main())
