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
    ("silence-30hz", 30, "silence", False),
    ("audio-ff-alone", 0, "audio_ff_once", False),
    ("audio-7f-alone", 0, "audio_7f_once", False),
    ("audio-stock-zero-alone", 0, "audio_stock_once", False),
    ("audio-tone-alone", 0, "audio_tone_once", False),
    ("audio-ff-after-clock", 30, "audio_ff_clock", False),
    ("audio-tone-after-clock", 30, "audio_tone_clock", False),
    ("audio-tone-isolated-datagram", 30, "audio_tone_isolated", False),
    ("camera-idle", 0, "idle", True),
    ("camera-display-sparse", 30, "image_sparse", True),
    ("camera-display-patterns", 30, "patterns", True),
]
BY_NAME = {c[0]: c for c in CASES}

def worker(name, directory, config, duration):
    import pycozmo
    from pycozmo import audio
    import hashlib
    import types
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
        def audio_probe(kind):
            if kind == "ff":
                payload = bytes([255])*744
            elif kind == "7f":
                payload = bytes([127])*744
            elif kind == "stock":
                payload = bytes(audio.bytes_to_cozmo(bytes(1488), 1, 1))
            else:
                payload = bytes(ulaw(int(1800*math.sin(2*math.pi*440*i/22320)))
                                for i in range(744))
            packet = p.OutputAudio(samples=payload)
            serialized = packet.to_bytes()
            result["audio_payload"] = {"length":len(serialized),
                "sha256":hashlib.sha256(serialized).hexdigest(),
                "first16":serialized[:16].hex(),"matches_input":serialized==payload}
            save()
            send(packet)
        if mode == "audio_tone_isolated":
            sender = cli.conn.send_thread
            original = sender._send_packets
            def isolated(self, packets, last_ack):
                for seq, packet in packets:
                    original([(seq, packet)], last_ack)
            sender._send_packets = types.MethodType(isolated, sender)
        if mode == "stock":
            cli.enable_animations(True)
        if rate == 0 and mode == "silence":
            send(p.OutputSilence())
        if rate == 0 and mode == "image":
            send(p.DisplayImage(image=encoded))
        if mode.endswith("_once") and mode.startswith("audio_"):
            audio_probe(mode.split("_")[1])
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
                    m["queued_app_commands"] > 12 or m["pending_reliable_packets"] >= 12):
                    result["status"] = "FAIL"
                    result["reason"] = "RX/ACK stalled or transport backlog exceeded limit"
                    break
            if rate and now >= next_frame:
                if mode.startswith("audio_") and frame == 30:
                    audio_probe(mode.split("_")[1])
                else:
                    send(p.OutputSilence())
                if mode == "image_sparse" and frame % 30 == 0:
                    send(p.DisplayImage(image=encoded))
                if mode == "patterns" and frame % 30 == 0:
                    img = Image.new("1",(128,32),0)
                    draw = ImageDraw.Draw(img)
                    if (frame//30)%3 == 0:
                        draw.rectangle((32,8,95,23),fill=1)
                    elif (frame//30)%3 == 1:
                        for x in range(0,128,16):
                            draw.rectangle((x,0,x+7,31),fill=1)
                    else:
                        draw.text((4,8),"BAT 3.85V TEST",fill=1)
                    send(p.DisplayImage(image=bytes(ImageEncoder(img).encode())))
                frame += 1
                # Never catch up by bursting after a delayed iteration.
                next_frame = max(next_frame + 1/rate, time.monotonic())
            time.sleep(.002)
        cli.enable_animations(False)
        # Stop experimental traffic and measure recovery independently.
        time.sleep(2)
        result["after"] = h._snapshot(cli)
        if result["status"] == "RUNNING":
            result["status"] = "PASS" if result["after"]["pending_reliable_packets"] == 0 and result["after"]["queued_app_commands"] == 0 else "FAIL"
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
            deps = [] if name=="idle-animation-state" else ["idle-animation-state"]
            if name.startswith("camera-display"):
                deps.append("camera-idle")
            if rate==30 and name!="silence-30hz":
                deps.append("silence-30hz")
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
                "audio_payload":r.get("audio_payload"),
                "tx_problems":[x for x in wire if x.get("problem") and x.get("direction")=="tx"][:5],
                "audio_wire":[x for x in wire if x.get("direction")=="tx" and any(p.get("id")==142 for p in x.get("packets",[]))][:4],
                "ack_tail":[{k:x.get(k) for k in ("kind","ack","first_seq","last_seq","bytes")} for x in wire if x.get("direction")=="rx"][-5:],
                "after":r.get("after"),
                "rx_problem_count":sum(bool(x.get("problem")) and x.get("direction")=="rx" for x in wire),
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
