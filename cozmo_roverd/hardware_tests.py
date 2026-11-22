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
import inspect
import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave

from .config import load
from .diagnostics import Recorder
from .wifi import Wifi
from .wire_diagnostics import WireRecorder, observe_socket, send_thread_exception_trace

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


def _pycozmo_transport_patched():
    """Check the *installed* implementation, not a claimed version number."""
    try:
        from pycozmo.conn import SendThread
        source = inspect.getsource(SendThread._send_packets)
        return "self._send_frame(to_frame, first_seq, last_seq, last_ack)" in source
    except (ImportError, OSError):
        return False



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
        "connect_attempt_failed": "Robot did not become ready on this connection attempt",
        "connect_retries_exhausted": "Robot connection failed after retries",
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
    reached_robot = any(e.get("event") == "robot_ready" for e in records)
    if returncode == 0 and any(e.get("event") == "test_finished" for e in records):
        status, reason = "PASS", "Completed without detected ACK stall"
    elif (not reached_robot and any(e.get("event") ==
             "connect_retries_exhausted" for e in records)):
        status, reason = "INCONCLUSIVE", "3 connection attempts failed; feature was never tested"
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


def _read_records(path):
    if not path.exists():
        return []
    records = []
    with path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _fmt_value(value):
    return "?" if value is None else str(value)


def _print_summary(case, result, directory):
    """Print evidence, not merely a verdict. The JSONL remains unabridged."""
    records = _read_records(directory / "measurements.jsonl")
    print(f"\n=== {case} ===", flush=True)
    print(f"Outcome: {result['status']} — {result['reason']} (exit {result['returncode']})")
    print("Events:")
    interesting = {
        "test_started", "connecting_wifi", "connect_attempt", "connect_attempt_failed",
        "connect_retries_exhausted", "robot_ready",
        "pre_command_transport", "pre_command_stable", "command_about_to_send",
        "command_issued", "stalled", "audio_not_reported_played",
        "outstanding_commands_at_end", "test_exception",
        "parent_killed_hung_worker", "test_finished", "cleanup_error",
    }
    command = next((r for r in records if r.get("event") == "command_issued"), None)
    start = next((r for r in records if r.get("event") == "pre_command_stable"), None)
    origin = (command or start or (records[0] if records else {})).get("utc")
    from datetime import datetime
    def relative(row):
        try:
            return (datetime.fromisoformat(row["utc"]) -
                    datetime.fromisoformat(origin)).total_seconds()
        except (KeyError, TypeError, ValueError):
            return None
    def when(row):
        offset = relative(row)
        return f"T{offset:+.2f}s" if offset is not None else "T+?"
    for row in records:
        if row.get("event") not in interesting:
            continue
        metrics = row.get("transport")
        details = []
        if row.get("action"):
            details.append(str(row["action"]))
        if row.get("message"):
            details.append(str(row["message"])[:250])
        if isinstance(metrics, dict):
            details.append("ACK=" + _fmt_value(metrics.get("next_ack_expected")))
            details.append("pending=" + _fmt_value(metrics.get("pending_reliable_packets")))
            details.append("queued=" + _fmt_value(metrics.get("queued_app_commands")))
            details.append("tx=" + _fmt_value(metrics.get("outgoing_packets")))
            details.append("rx_frames=" + _fmt_value(metrics.get("received_frames")))
            details.append("audio_played=" + _fmt_value(metrics.get("audio_frames_reported_played")))
        print(f"  {when(row):>9} {row['event']}: " + ", ".join(details))
    samples = [r for r in records if r.get("event") == "sample"
               and isinstance(r.get("transport"), dict)]
    if samples:
        print(f"Transport progression ({len(samples)} real samples, shown at "
              "roughly 1s intervals plus changes near failure):")
        last_at = float("-inf")
        selected = []
        for row in samples:
            t = relative(row)
            if t is None:
                continue
            metrics = row["transport"]
            prev = selected[-1]["transport"] if selected else {}
            critical = (metrics.get("pending_reliable_packets", 0) >= 50
                        and prev.get("pending_reliable_packets", 0) < 50)
            if t - last_at >= 0.95 or critical or row is samples[-1]:
                selected.append(row)
                last_at = t
        for row in selected:
            m = row["transport"]
            print(
                f"  {when(row):>9} ACK={_fmt_value(m.get('next_ack_expected')):>5}"
                f" pending={_fmt_value(m.get('pending_reliable_packets')):>3}/62"
                f" queue={_fmt_value(m.get('queued_app_commands')):>5}"
                f" tx={_fmt_value(m.get('outgoing_packets')):>5}"
                f" retry_tx={_fmt_value(m.get('sent_packets_including_retries')):>5}"
                f" rx_frames={_fmt_value(m.get('received_frames')):>5}"
                f" rx_packets={_fmt_value(m.get('received_packets')):>5}"
                f" audio={_fmt_value(m.get('audio_frames_reported_played')):>4}"
            )
        # The stall is a lack of outbound ACK progress, not necessarily Wi-Fi
        # disconnection. Explicitly report independent inbound progress.
        first = samples[0]["transport"]
        last = samples[-1]["transport"]
        rx_change = last.get("received_frames", 0) - first.get("received_frames", 0)
        ack_change = ((last.get("next_ack_expected", 0) -
                       first.get("next_ack_expected", 0)) % 65536)
        print(f"Observed during test: ACK advanced {ack_change} sequence values; "
              f"received {rx_change} additional inbound frames.")
        print(f"Final queued={last.get('queued_app_commands', '?')}, "
              f"pending={last.get('pending_reliable_packets', '?')}/62, "
              f"audio frames reported={last.get('audio_frames_reported_played', '?')}.")
    # Packet events are captured by pycozmo itself and sanitized by Recorder.
    # Show transmitted command types (including repeated audio/silence),
    # grouped, plus the exact most recent command sequence.
    packet_records = []
    incident_path = directory / "incidents.jsonl"
    for row in reversed(_read_records(incident_path)):
        if row.get("event") == "disconnection_snapshot":
            packet_records = row.get("recent_protocol_packets", [])
            break
    if not packet_records:
        for line in (directory / "protocol.log").read_text(
                encoding="utf-8", errors="replace").splitlines() if (
                    directory / "protocol.log").exists() else []:
            if "wire tx " in line:
                packet_records.append({"direction": "tx", "packet":
                    line.split("wire tx ", 1)[1].split(" bytes=", 1)[0],
                    "utc": line.split(" ", 1)[0]})
    sent = [p for p in packet_records if p.get("direction") == "tx"]
    if sent:
        from collections import Counter
        types = Counter(p.get("packet", "unknown") for p in sent)
        print("Recent outgoing packet types: " +
              ", ".join(f"{name}={count}" for name, count in types.most_common(12)))
        print("Last 20 queued-to-send packet types (not proof of robot receipt):")
        for p in sent[-20:]:
            print(f"  {p.get('utc', '?')}  {p.get('packet', '?')} "
                  f"id={p.get('id', '?')} bytes={p.get('bytes', '?')}")
    elif result["status"] == "FAIL":
        print("Packet trace: no parsed outgoing packet metadata available.")
    wire = _read_records(directory / "wire-frames.jsonl")
    if wire:
        tx = [r for r in wire if r.get("direction") == "tx"]
        rx = [r for r in wire if r.get("direction") == "rx"]
        problems = [r for r in wire if r.get("problem")]
        exceptions = [r for r in wire if r.get("kind") in
                      ("send_thread_exception", "socket_send_error")]
        repeated = 0
        seen = set()
        for r in tx:
            key = (r.get("first_seq"), r.get("last_seq"))
            if r.get("kind") == "ENGINE" and key in seen:
                repeated += 1
            elif r.get("kind") == "ENGINE":
                seen.add(key)
        print(f"Actual UDP frames: tx={len(tx)} rx={len(rx)} "
              f"duplicate tx sequence ranges={repeated}; "
              f"header/payload problems={len(problems)}; "
              f"sender exceptions={len(exceptions)}")
        if problems:
            print("FIRST WIRE MISMATCHES:")
            for r in problems[:12]:
                print("  " + json.dumps(r, separators=(",", ":")))
        if exceptions:
            print("SEND THREAD ERRORS:")
            for r in exceptions[:12]:
                print("  " + json.dumps(r, separators=(",", ":")))
        print("LAST 16 OUTBOUND UDP FRAME HEADERS (actual socket sends):")
        for r in tx[-16:]:
            print(f"  {r.get('utc','?')} {r.get('kind','?')} "
                  f"seq={r.get('first_seq','?')}..{r.get('last_seq','?')} "
                  f"ack={r.get('ack','?')} bytes={r.get('bytes','?')} "
                  f"packets={r.get('packets',[])}"
                  + (f" PROBLEM={r['problem']}" if r.get("problem") else ""))
        print("LAST 8 INBOUND UDP FRAME HEADERS:")
        for r in rx[-8:]:
            print(f"  {r.get('utc','?')} {r.get('kind','?')} "
                  f"seq={r.get('first_seq','?')}..{r.get('last_seq','?')} "
                  f"ack={r.get('ack','?')} bytes={r.get('bytes','?')}")
    else:
        print("Actual UDP-frame trace unavailable (no frames recorded).")
    print(f"Full records: {directory}", flush=True)


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
    wire = WireRecorder(test_dir)
    cli = None
    original_thread_trace = threading.gettrace()

    def write(event, **fields):
        with report.open("a", encoding="utf-8") as fd:
            fd.write(json.dumps({
                "utc": datetime.now(timezone.utc).isoformat(),
                "event": event, "case": case, **fields}, default=str) + "\n")
            fd.flush()
            os.fsync(fd.fileno())

    write("test_started", pycozmo_module=pycozmo.__file__,
          corrected_frame_split=_pycozmo_transport_patched())
    try:
        if _service_active():
            raise RuntimeError("Production cozmo-roverd.service is running; stop it before testing")
        cfg = load(config_path)
        write("connecting_wifi", interface=cfg.interface)
        Wifi(cfg).wait()
        animations = case.startswith("audio") or "animation" in case
        # Each connection attempt gets a fresh client/socket. A handshake
        # timeout is not evidence of an audio or lighting failure.
        # The original bridge service stays stopped for all attempts.
        for attempt in range(1, 4):
            write("connect_attempt", attempt=attempt)
            cli = pycozmo.Client(
                enable_animations=animations, enable_procedural_face=False)
            observe_socket(cli, wire)
            threading.settrace(send_thread_exception_trace(wire))
            try:
                cli.start()
                cli.connect()
                cli.wait_for_robot(timeout=12)
                break
            except Exception as exc:
                write("connect_attempt_failed", attempt=attempt,
                      exception_type=type(exc).__name__, message=str(exc))
                retryable = type(exc).__name__ in ("ConnectionTimeout", "Timeout")
                try:
                    cli.stop()
                except Exception as close_exc:
                    write("failed_connect_cleanup_error", attempt=attempt,
                          message=str(close_exc))
                cli = None
                if not retryable:
                    raise
                if attempt == 3:
                    write("connect_retries_exhausted", attempts=attempt)
                    recorder.incident(reason="robot_handshake_failed_3_times",
                                      stage=case)
                    return 6
                time.sleep(2)
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
        threading.settrace(original_thread_trace)
        wire.close()
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
    if not _pycozmo_transport_patched():
        parser.error("Installed pycozmo still has the old frame-splitting bug. "
                     "Run: sudo ./install-fedora.sh from the current checkout")
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
        print("PyCozmo split-frame sequence correction: VERIFIED installed", flush=True)
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
                print("INTERPRETATION: animation+camera fails after connecting, "
                      "so audio is not the only failing condition.", flush=True)
            elif b == "INCONCLUSIVE":
                print("INTERPRETATION: animation-only control never reached the "
                      "robot; no conclusion about whether audio causes the stall.",
                      flush=True)
        if "backpack-side-camera" in results:
            print("LIGHTS: " + ("side packet ACKed in this test" if
                  results["backpack-side-camera"]["status"] == "PASS" else
                  "side-light test did not establish a stable ACKed result"), flush=True)
        print(f"Evidence kept: {suite_dir}", flush=True)
        return 1 if any(x["status"] in ("FAIL", "INCONCLUSIVE")
                        for x in results.values()) else 0
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
