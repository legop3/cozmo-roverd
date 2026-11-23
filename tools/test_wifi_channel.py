#!/usr/bin/env python3
"""Manual Cozmo radio-channel experiment. Never runs during normal roverd operation.

The official engine-to-robot message SetBodyRadioMode (0x07) contains
radioMode (int8) and wifiChannel (uint8). The normal accessory mode is 1.
Whether this changes the ESP8266 Wi-Fi AP channel is NOT established.
"""
from __future__ import annotations

import argparse
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Send experimental SetBodyRadioMode to Cozmo")
    parser.add_argument("--channel", type=int, default=5,
                        help="Requested Wi-Fi channel (default: 5)")
    parser.add_argument("--observe-seconds", type=float, default=10,
                        help="Keep the client alive after sending (default: 10)")
    args = parser.parse_args()
    if not 1 <= args.channel <= 11:
        parser.error("--channel must be between 1 and 11")
    if not 0 <= args.observe_seconds <= 120:
        parser.error("--observe-seconds must be between 0 and 120")

    # Import after parsing so --help doesn't require PyCozmo installed.
    import pycozmo
    from pycozmo.protocol_base import UnknownCommand

    print("EXPERIMENTAL: this may disconnect Cozmo or change its body radio mode.")
    print("Stop cozmo-roverd.service before running; it must not share Cozmo's connection.")
    print(f"Connecting to Cozmo; will send SetBodyRadioMode(mode=1, channel={args.channel})...")
    cli = pycozmo.Client(enable_animations=False, enable_procedural_face=False)
    cli.start()
    try:
        cli.connect()
        cli.wait_for_robot(timeout=12)
        # Body accessory mode 1, followed by the requested uint8 channel.
        payload = bytes((1, args.channel))
        cli.conn.send(UnknownCommand(0x07, payload))
        print(f"Queued command 0x07, payload {payload.hex(' ')}.")
        print("Watch Cozmo's AP channel now. Queued does NOT mean accepted or applied.")
        time.sleep(args.observe_seconds)
    finally:
        # Do not issue WifiOff or ShutdownRobot as part of the experiment.
        cli.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
