#!/usr/bin/env python3
"""Manual Cozmo radio-channel experiment. Never runs during normal roverd operation.

Official SetBodyRadioMode (0x07) has radioMode (int8) and wifiChannel
(uint8). Accessory radio mode is 1. Whether this changes Cozmo's Wi-Fi
access-point channel is unknown. Run only while physically able to
recover the robot if it disconnects.
"""
from __future__ import annotations

import argparse
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Experimentally send SetBodyRadioMode to Cozmo")
    parser.add_argument("--channel", type=int, default=5,
                        help="Requested channel (default: 5)")
    parser.add_argument("--observe-seconds", type=float, default=90,
                        help="Observe after first send, in seconds (default: 90)")
    parser.add_argument("--repeats", type=int, default=3,
                        help="Number of sends (default: 3)")
    parser.add_argument("--interval-seconds", type=float, default=10,
                        help="Seconds between sends (default: 10)")
    args = parser.parse_args()

    if not 1 <= args.channel <= 11:
        parser.error("--channel must be between 1 and 11")
    if not 1 <= args.repeats <= 20:
        parser.error("--repeats must be between 1 and 20")
    if not 1 <= args.interval_seconds <= 120:
        parser.error("--interval-seconds must be between 1 and 120")
    if not 0 <= args.observe_seconds <= 600:
        parser.error("--observe-seconds must be between 0 and 600")
    if (args.repeats - 1) * args.interval_seconds > args.observe_seconds:
        parser.error("Repeated sends must fit inside --observe-seconds")

    import pycozmo
    from pycozmo.protocol_base import UnknownCommand

    print("EXPERIMENTAL: this command may disconnect Cozmo or alter its body radio mode.", flush=True)
    print("Stop cozmo-roverd.service before running; do not run two clients.", flush=True)
    print(f"Connecting; requesting channel {args.channel}, mode 1; "
          f"{args.repeats} sends over {args.observe_seconds:g} seconds.", flush=True)
    cli = pycozmo.Client(enable_animations=False, enable_procedural_face=False)
    cli.start()
    try:
        cli.connect()
        cli.wait_for_robot(timeout=12)
        payload = bytes((1, args.channel))
        start = time.monotonic()
        print("Watch Cozmo's AP channel. Queued does NOT mean applied.", flush=True)
        for attempt in range(args.repeats):
            remaining = (start + attempt * args.interval_seconds) - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            cli.conn.send(UnknownCommand(0x07, payload))
            print(f"[{time.monotonic() - start:5.1f}s] Queued send "
                  f"{attempt + 1}/{args.repeats}: 0x07 {payload.hex(' ')}", flush=True)
        remaining = start + args.observe_seconds - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        print("Observation finished.", flush=True)
    finally:
        # Do not issue WifiOff or ShutdownRobot.
        cli.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
