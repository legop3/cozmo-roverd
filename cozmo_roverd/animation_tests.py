"""Unattended, isolated animation transport probes (no production changes)."""
import argparse
import json
import math
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import hardware_tests as h

# Every condition receives a fresh connection. No automatic escalation after
# a failed lower-rate prerequisite in the same family.
CASES = [
    ("idle-animation-state", 0, "idle", False),
    ("idle-no-animation-state", 0, "idle", False),
    ("silence-once", 0, "silence", False),
    *[(f"silence-{r}hz", r, "silence", False) for r in (1, 5, 10, 15, 30)],
    ("image-once-no-clock", 0, "image", False),
    ("image-once-with-clock", 30, "image_once", False),
    ("image-1hz-with-clock", 30, "image_sparse", False),
    ("image-30hz-with-clock", 30, "image", False),
    ("audio-silent-frame", 30, "audio_silent", False),
    ("audio-tone-100ms", 30, "tone3", False),
    ("audio-tone-1s", 30, "tone30", False),
    ("image-and-tone", 30, "combined", False),
    ("stock-controller", 0, "stock", False),
    ("camera-idle", 0, "idle", True),
    ("camera-silence-30hz", 30, "silence", True),
    ("camera-image-and-tone", 30, "combined", True),
]
BY_NAME = {c[0]: c for c in CASES}

def worker(name, directory, config, duration):
    import pycozmo
    from pycozmo import protocol_encoder as p
    from pycozmo.image_encoder import ImageEncoder
    from PIL import Image, ImageDraw
    from .wire_diagnostics import WireRecorder, observe_socket
    from .wifi import Wifi
    from .config import load
    _, rate, mode, camera = BY_NAME[name]
    directory = Path(directory)
    wire = WireRecorder(directory)
    cli = None
    report = directory / "result.json"
    result = {"case": name, "status": "INCONCLUSIVE", "sent": {},
              "samples": [], "pycozmo": pycozmo.__file__}
    counts = result["sent"]
    def save():
        report.write_text(json.dumps(result, indent=2, default=str))
    try:
        if h._service_active():
            raise RuntimeError("Production bridge still running")
        Wifi(load(config)).wait()
        result["connection_attempts"] = []
        for attempt in range(1, 5):
            cli = pycozmo.Client(enable_animations=False, enable_procedural_face=False)
            if name == "idle-no-animation-state":
                cli.anim_controller.start = lambda: None
            observe_socket(cli, wire)
            entry = {"attempt": attempt}
            result["connection_attempts"].append(entry)
            save()
            try:
                cli.start()
                cli.connect()
                cli.wait_for_robot(timeout=12)
                entry["status"] = "ready"
                save()
                break
            except Exception as exc:
                entry["error"] = repr(exc)
                save()
                cli.stop()
                cli = None
                if type(exc).__name__ not in ("ConnectionTimeout", "Timeout") or attempt == 4:
                    raise
                time.sleep(5)
        cli.enable_camera(enable=camera, color=camera)
        time.sleep(3)
        result["status"] = "RUNNING"
        before = h._snapshot(cli)
        result["before"] = before
        save()
        image = Image.new("1", (128, 32), 0)
        ImageDraw.Draw(image).rectangle((32, 8, 95, 23), fill=1)
        encoded = bytes(ImageEncoder(image).encode())
        result["image_bytes"] = len(encoded)
        # G.711 mu-law, generated locally, no external audio tools.
        def ulaw(s):
            sign = 128 if s < 0 else 0
            mag = min(abs(s), 32635) + 132
            exponent = max(0, mag.bit_length() - 8)
            mantissa = (mag >> (exponent + 3)) & 15
            return (~(sign | (exponent << 4) | mantissa)) & 255
        def send(packet):
            cli.conn.send(packet)
            key = type(packet).__name__
            counts[key] = counts.get(key, 0) + 1
        if mode == "stock":
            cli.enable_animations(True)
        if rate == 0 and mode == "silence":
            send(p.OutputSilence())
        if rate == 0 and mode == "image":
            send(p.DisplayImage(image=encoded))
        start = time.monotonic()
        next_frame = start
        next_sample = start
        frame = 0
        prev = before
        last_rx = last_ack = start
        ack = before["next_ack_expected"]
        while time.monotonic() - start < duration:
            now = time.monotonic()
            if now >= next_sample:
                m = h._snapshot(cli)
                if m["received_frames"] != prev["received_frames"]:
                    last_rx = now
                if m["next_ack_expected"] != ack or not m["pending_reliable_packets"]:
                    last_ack = now
                ack = m["next_ack_expected"]
                prev = m
                result["samples"].append({"t": round(now-start, 3), **m,
                    "animation_bytes_played": getattr(cli,"num_anim_bytes_played",None),
                    "animation_drop_count": getattr(cli,"client_drop_count",None)})
                save()
                next_sample = now + .25
                if (now-last_rx > 3 or now-last_ack > 3 or
                    m["queued_app_commands"] > 60 or m["pending_reliable_packets"] >= 60):
                    result["status"] = "FAIL"
                    result["reason"] = "RX/ACK stalled or transport backlog exceeded limit"
                    break
            if rate and now >= next_frame:
                n = {"audio_silent":1,"tone3":3,"tone30":30,"combined":30}.get(mode,0)
                if frame < n:
                    samples = bytes([255]*744) if mode == "audio_silent" else bytes(
                        ulaw(int(1800*math.sin(2*math.pi*440*(frame*744+i)/22320)))
                        for i in range(744))
                    send(p.OutputAudio(samples=samples))
                else:
                    send(p.OutputSilence())
                if (mode in ("image","combined") or
                    (mode=="image_once" and frame==0) or
                    (mode=="image_sparse" and frame%30==0)):
                    send(p.DisplayImage(image=encoded))
                frame += 1
                # Never catch up by bursting after a delayed iteration.
                next_frame = max(next_frame + 1/rate, time.monotonic())
            time.sleep(.002)
        cli.enable_animations(False)
        # Stop experimental traffic and measure recovery independently.
        time.sleep(2)
        result["after"] = h._snapshot(cli)
        if result["status"] == "RUNNING":
            result["status"] = "PASS" if result["after"]["pending_reliable_packets"] < 6 else "FAIL"
            result["reason"] = "Transport completed; physical output not verified"
        save()
    except Exception as exc:
        if result["status"] == "RUNNING":
            result["status"] = "FAIL"
        result["reason"] = repr(exc)
        save()
    finally:
        if cli is not None:
            cli.stop()
        wire.close()
    return 0

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="/etc/cozmo-roverd.toml")
    ap.add_argument("--duration", type=int, default=12)
    ap.add_argument("--worker", choices=BY_NAME)
    ap.add_argument("--directory")
    args = ap.parse_args()
    if args.worker:
        return worker(args.worker, args.directory, args.config, args.duration)
    if not h._pycozmo_transport_patched():
        ap.error("Installed PyCozmo transport patch not detected")
    if not 5 <= args.duration <= 120:
        ap.error("duration must be 5..120 seconds")
    root = Path("/var/log/cozmo-roverd/animation-tests") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root.mkdir(parents=True)
    active = h._service_active()
    results = {}
    try:
        if active:
            h._service_action("stop")
        if h._service_active():
            raise RuntimeError("Could not stop bridge")
        print("Waiting for previous bridge session to expire...", flush=True)
        time.sleep(10)
        for name, rate, mode, camera in CASES:
            deps = []
            if name not in ("idle-no-animation-state", "idle-animation-state"):
                deps.append("idle-animation-state")
            if name.startswith("silence-") and rate:
                rates = [0,1,5,10,15,30]
                prev = rates[rates.index(rate)-1]
                deps.append("silence-once" if prev==0 else f"silence-{prev}hz")
            if rate==30 and not name.startswith("silence-"):
                deps.append("silence-30hz")
            if name in ("image-1hz-with-clock","image-30hz-with-clock"):
                deps.append("image-once-with-clock" if name=="image-1hz-with-clock" else "image-1hz-with-clock")
            if name.startswith("audio-tone"):
                deps.append("audio-silent-frame" if name.endswith("100ms") else "audio-tone-100ms")
            if mode=="combined":
                deps.extend(["audio-tone-1s","image-30hz-with-clock"])
            if camera and name!="camera-idle":
                deps.append("camera-idle")
            if name=="camera-image-and-tone":
                deps.append("camera-silence-30hz")
            blocked = [d for d in deps if results.get(d)!="PASS"]
            if blocked:
                results[name]="SKIP"
                print(f"{name}: SKIP prerequisites {blocked}",flush=True)
                continue
            d = root/name
            d.mkdir()
            print(f"Testing {name}...",flush=True)
            timedout=False
            with (d/"console.log").open("w") as log:
                try:
                    proc=subprocess.run([sys.executable,"-m","cozmo_roverd.animation_tests","--worker",name,
                        "--directory",str(d),"--config",args.config,"--duration",str(args.duration)],
                        stdout=log,stderr=subprocess.STDOUT,timeout=args.duration+150)
                except subprocess.TimeoutExpired:
                    timedout=True
            try:
                r=json.loads((d/"result.json").read_text())
            except (OSError,ValueError):
                r={"status":"INCONCLUSIVE","reason":"No readable worker report"}
            if timedout or (not timedout and proc.returncode):
                r["status"]="INCONCLUSIVE" if not r.get("before") else "FAIL"
                r["reason"]="Worker timeout or nonzero exit"
            results[name]=r["status"]
            s=r.get("samples",[])
            a=r.get("before",{})
            b=r.get("after",s[-1] if s else {})
            wire=h._read_records(d/"wire-frames.jsonl")
            summary={"case":name,"status":r["status"],"reason":r.get("reason"),
                "connection_attempts":r.get("connection_attempts"),
                "sent":r.get("sent"),"rx_delta":b.get("received_frames",0)-a.get("received_frames",0),
                "audio_delta":b.get("audio_frames_reported_played",0)-a.get("audio_frames_reported_played",0),
                "max_pending":max((x["pending_reliable_packets"] for x in s),default=None),
                "max_queue":max((x["queued_app_commands"] for x in s),default=None),
                "wire_tx":sum(x.get("direction")=="tx" for x in wire),
                "wire_rx":sum(x.get("direction")=="rx" for x in wire),
                "wire_problems":[x for x in wire if x.get("problem")][:3],
                "last_sample":s[-1] if s else None}
            print(json.dumps(summary,default=str),flush=True)
            if r["status"]=="INCONCLUSIVE" or not r.get("before"):
                print("Worker log tail:",(d/"console.log").read_text(errors="replace")[-2000:],flush=True)
            if not r.get("before"):
                print("Wire tail:",json.dumps(wire[-5:],default=str),flush=True)
                for command in (["ip","route","get","172.31.1.1"],
                                ["ping","-c","5","-W","1","172.31.1.1"],
                                ["iw","dev"]):
                    try:
                        probe=subprocess.run(command,capture_output=True,text=True,timeout=12)
                        print("Network probe:", " ".join(command),probe.returncode,
                              probe.stdout,probe.stderr,flush=True)
                    except (OSError,subprocess.TimeoutExpired) as exc:
                        print("Network probe error:",repr(exc),flush=True)
            time.sleep(5)
        print("=== ANIMATION TEST RESULTS ===",flush=True)
        for name,status in results.items():
            print(f"{status:12} {name}",flush=True)
        print(f"Full evidence: {root}",flush=True)
    finally:
        if active:
            h._service_action("start")
            print("Production bridge restarted.",flush=True)
    return int(any(v in ("FAIL","INCONCLUSIVE") for v in results.values()))

if __name__ == "__main__":
    sys.exit(main())
