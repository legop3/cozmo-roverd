"""Opt-in *physical* Cozmo fault-isolation probes using unmodified PyCozmo.

Each case runs in a separate, killable process, never as a bridge command.
The runner stops and restores the production bridge automatically. Tests send
real hardware commands and can still cause a physical Cozmo firmware hang.
Results and diagnoses print directly in the terminal, with full logs retained.
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
    "baseline-animation", "baseline-animation-camera",
    "backpack-stock", "backpack-side", "backpack-side-camera",
    "charger", "audio-idle", "audio-camera",
)

DEFAULT_SUITE = (
    "baseline-camera", "baseline-animation-camera",
    "backpack-side-camera", "audio-idle", "audio-camera",
)
SERVICE = "cozmo-roverd.service"


def _service_active():
    return subprocess.run(
        ["systemctl", "is-active", "--quiet", SERVICE],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        check=False).returncode == 0


def _service_action(action):
    result = subprocess.run(["systemctl", action, SERVICE],
                            capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(
            f"Cannot {action} {SERVICE}: "
            f"{result.stderr.strip() or result.stdout.strip()}")


def _summarize(directory, returncode):
    """Generate an operator-readable verdict from the actual worker records."""
    path = directory / "measurements.jsonl"
    if not path.exists():
        return {"status": "FAIL", "reason": "No measurements (worker failed before opening log)",
                "returncode": returncode}
    records = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # Include previous complete records if process was killed mid-write.
    last = records[-1] if records else {}
    issue_names = {
        "stalled": "ACK progress stopped; reliable send window stalled",
        "audio_not_reported_played": "Robot reported no audio playback",
        "outstanding_commands_at_end": "Commands still waiting for ACKs",
        "test_exception": "Hardware test raised an exception",
        "parent_killed_hung_worker": "Worker hung and was killed by watchdog",
        "cleanup_error": "Cleanup failed",
        "command_issued": "Test stopped immediately after sending the command",
    }
    issue = next((e for e in reversed(records)
                  if e.get("event") in issue_names), None)
    skipped = next((e for e in reversed(records)
                    if str(e.get("action", "")).startswith("SKIPPED")), None)
    sample = next((e for e in reversed(records)
                   if isinstance(e.get("transport"), dict)), {})
    start = next((e for e in records if e.get("event") == "pre_command_transport"), {})
    initial = start.get("transport", {})
    transport = sample.get("transport", {})
    if returncode == 0 and any(e.get("event") == "test_finished" for e in records):
        status, reason = "PASS", "Completed without detected ACK stall"
    elif returncode == 2 or skipped is not None:
        status, reason = "SKIP", (skipped or {}).get("action", "Not applicable")
    else:
        status = "FAIL"
        reason = (issue_names[issue["event"]] if issue else
                  last.get("message", "Incomplete run / unexpected worker exit"))
        if issue and issue.get("message"):
            reason += ": " + str(issue["message"])[:160]
    result = {"status": status, "reason": reason,
              "returncode": returncode,
              "pending": transport.get("pending_reliable_packets"),
              "queued": transport.get("queued_app_commands"),
              "ack_before": initial.get("next_ack_expected"),
              "ack_after": transport.get("next_ack_expected"),
              "received_frames": transport.get("received_frames"),
              "audio_frames": (transport.get("audio_frames_reported_played", 0)
                               - initial.get("audio_frames_reported_played", 0))}
    return result


def _print_summary(case, result, directory):
    print(f"  {result['status']}: {case} — {result['reason']}", flush=True)
    if result.get("pending") is not None:
        print(f"     ACK {result['ack_before']} -> {result['ack_after']} | "
              f"pending {result['pending']} | queued {result['queued']} | "
              f"received frames {result['received_frames']} | "
              f"audio frames {result['audio_frames']}", flush=True)
    print(f"     Detailed evidence: {directory}", flush=True)


def _run_case(case, args, suite_dir):
    directory = suite_dir / case
    directory.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "cozmo_roverd.hardware_tests", "--_worker",
               "--case", case, "--config", args.config,
               "--duration", str(args.duration), "--output-dir", str(directory)]
    print(f"\nTesting {case}...", flush=True)
    try:
        # Stream logs only into files. Summarize the failure in the terminal.
        with (directory / "worker-console.log").open("w", encoding="utf-8") as stream:
            result = subprocess.run(command, timeout=args.duration + 90,
                                    stdout=stream, stderr=subprocess.STDOUT,
                                    check=False)
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        with (directory / "measurements.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": "parent_killed_hung_worker",
                                     "case": case}) + "\n")
        returncode = 124
    summary = _summarize(directory, returncode)
    _print_summary(case, summary, directory)
    return summary


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
        if _service_active():
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
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--case", choices=CASES, help="Run one hardware test")
    selection.add_argument("--suite", action="store_true",
                           help="Run the relevant baselines, lighting and audio comparisons")
    parser.add_argument("--config", default="/etc/cozmo-roverd.toml")
    parser.add_argument("--duration", type=int, default=15)
    parser.add_argument("--output-dir", default="/var/log/cozmo-roverd/hardware-tests")
    parser.add_argument("--i-understand-risk", action="store_true",
                        help="Required: audio/light tests can hang the physical robot")
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not 3 <= args.duration <= 120:
        parser.error("--duration must be 3..120 seconds")
    if not args._worker and not args.i_understand_risk:
        parser.error("Requires --i-understand-risk: tests send real robot packets")
    if args._worker:
        if args.case is None:
            parser.error("Internal worker needs --case")
        return _worker(args.config, args.case, args.duration, args.output_dir)

    cases = (args.case,) if args.case else DEFAULT_SUITE
    root = Path(args.output_dir)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    suite_dir = root / (run_id + "-diagnostics")
    suite_dir.mkdir(parents=True, exist_ok=False)

    service_was_running = _service_active()
    results = {}
    try:
        if service_was_running:
            print(f"Stopping {SERVICE} temporarily...", flush=True)
            _service_action("stop")
            if _service_active():
                raise RuntimeError("Rover service is still running; refusing to share robot")
        print("Cozmo diagnostic tests (real hardware)", flush=True)
        print(f"Log directory: {suite_dir}", flush=True)
        for case in cases:
            results[case] = _run_case(case, args, suite_dir)
        print("\n=== COZMO HARDWARE RESULTS ===", flush=True)
        for case, outcome in results.items():
            print(f"{outcome['status']:4} {case:27} {outcome['reason']}", flush=True)
        if "baseline-animation-camera" in results and "audio-camera" in results:
            b = results["baseline-animation-camera"]["status"]
            a = results["audio-camera"]["status"]
            if b == "PASS" and a == "FAIL":
                print("INTERPRETATION: animation+camera is stable; audio introduces a failure.",
                      flush=True)
            elif b == "FAIL":
                print("INTERPRETATION: animation+camera fails without audio; "
                      "do not attribute its failure solely to audio.", flush=True)
        if "backpack-side-camera" in results:
            print("LIGHTS: " + ("side packet ACKed in this test" if
                  results["backpack-side-camera"]["status"] == "PASS" else
                  "side-light test did not establish a stable ACKed result"), flush=True)
        print(f"Evidence kept: {suite_dir}", flush=True)
        return 1 if any(x["status"] == "FAIL" for x in results.values()) else 0
    finally:
        if service_was_running:
            print(f"Restoring {SERVICE}...", flush=True)
            try:
                _service_action("start")
                print("Rover service restarted.", flush=True)
            except Exception as exc:
                print(f"ERROR restarting rover: {exc}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    sys.exit(main())
