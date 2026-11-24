#!/usr/bin/env python3
"""Manual Cozmo radio/power experiments; not used by the rover daemon.

IMPORTANT: Some packets can make Cozmo unreachable. Run only with physical
access to the robot and after stopping cozmo-roverd.service.

The official SetBodyRadioMode message is 0x07 with two bytes. The published
PyCozmo ShutdownRobot is a zero-byte command but the reconstructed official
CLAD message is 32 bytes: the shutdown-legacy experiment deliberately tests
only the existing PyCozmo packet, not an invented 32-byte body.
"""
from __future__ import annotations

import argparse
import time

COMMANDS = {
    "radio": ("SetBodyRadioMode", 0x07),
    "wifi-off": ("WifiOff(enable=1)", 0xAE),
    "wifi-on": ("WifiOff(enable=0)", 0xAE),
    "sleep": ("EnterSleepMode", 0x52),
    "shutdown-legacy": ("PyCozmo ShutdownRobot (zero-byte version)", 0xA9),
    "wifi-telemetry": ("EnableWiFiTelemetry", 0x82),
}
DISRUPTIVE = {"wifi-off", "wifi-on", "sleep", "shutdown-legacy"}


def packet_for(command: str, channel: int) -> tuple[int, bytes]:
    """Return the command tag and raw payload; no network side effects."""
    command_id = COMMANDS[command][1]
    payload = {
        "radio": bytes((1, channel)),
        "wifi-off": bytes((1,)),
        "wifi-on": bytes((0,)),
        "sleep": b"",
        "shutdown-legacy": b"",
        "wifi-telemetry": b"",
    }[command]
    return command_id, payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Manually test Cozmo Wi-Fi and power commands; never automatic")
    parser.add_argument("command", nargs="?", choices=tuple(COMMANDS), default="radio",
                        help="Which command to test (default: radio)")
    parser.add_argument("--channel", type=int, default=5,
                        help="Requested channel for radio command (default: 5)")
    parser.add_argument("--observe-seconds", type=float, default=90,
                        help="Time after first send before exit (default: 90 seconds)")
    parser.add_argument("--repeats", type=int, default=None,
                        help="Number of sends (default: 3 radio, 1 otherwise)")
    parser.add_argument("--interval-seconds", type=float, default=10,
                        help="Seconds between sends (default: 10)")
    parser.add_argument("--confirm-risk", action="store_true",
                        help="Required for potentially connection-ending commands")
    args = parser.parse_args()

    if not 1 <= args.channel <= 11:
        parser.error("--channel must be between 1 and 11")
    repeats = args.repeats if args.repeats is not None else (3 if args.command == "radio" else 1)
    if not 1 <= repeats <= 20:
        parser.error("--repeats must be between 1 and 20")
    if args.command in DISRUPTIVE and repeats > 1:
        parser.error("Potentially disruptive commands can only be sent once per invocation")
    if not 1 <= args.interval_seconds <= 120:
        parser.error("--interval-seconds must be between 1 and 120")
    if not 0 <= args.observe_seconds <= 600:
        parser.error("--observe-seconds must be between 0 and 600")
    if (repeats - 1) * args.interval_seconds > args.observe_seconds:
        parser.error("Repeated sends must fit inside --observe-seconds")
    if args.command in DISRUPTIVE and not args.confirm_risk:
        parser.error(f"{args.command} can disconnect or shut down Cozmo; "
                     "run again with --confirm-risk if Cozmo is physically accessible")

    import pycozmo
    from pycozmo.protocol_base import UnknownCommand

    command_id, payload = packet_for(args.command, args.channel)
    label = COMMANDS[args.command][0]
    print(f"EXPERIMENTAL {label}; packet 0x{command_id:02x} / payload {payload.hex(' ') or '(empty)'}",
          flush=True)
    print("Stop cozmo-roverd.service first; do not connect two clients.", flush=True)
    if args.command == "shutdown-legacy":
        print("WARNING: Official CLAD describes 32 payload bytes for 0xA9; "
              "PyCozmo uses zero. This test uses the PyCozmo form.", flush=True)
    if args.command.startswith("wifi-"):
        print("WiFiOff's 'enable' flag meaning and reconnect behavior are unverified.", flush=True)

    client = pycozmo.Client(enable_animations=False, enable_procedural_face=False)
    client.start()
    try:
        client.connect()
        client.wait_for_robot(timeout=12)
        start = time.monotonic()
        for attempt in range(repeats):
            remaining = start + attempt * args.interval_seconds - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            client.conn.send(UnknownCommand(command_id, payload))
            print(f"[{time.monotonic() - start:5.1f}s] Queued {attempt + 1}/{repeats}: "
                  f"0x{command_id:02x} {payload.hex(' ')}", flush=True)
        print("Queued is NOT proof the robot received or applied the command.", flush=True)
        remaining = start + args.observe_seconds - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        print("Observation finished.", flush=True)
    finally:
        # Never automatically try to undo a Wi-Fi command over a lost connection.
        client.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
