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
    ("mtu1500-idle", 0, "idle", False),
    ("mtu1500-speech", 30, "speech", False),
    ("mtu1500-real-face", 30, "real_face", True),
    ("mtu1500-speech-face", 30, "speech_face", True),
    ("mtu1500-speech-face-soak", 30, "speech_face", True),
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
    original_mtu = None
    interface = None
    report = directory / "result.json"
    result = {"case": name, "status": "INCONCLUSIVE", "sent": {},
              "samples": [], "pycozmo": pycozmo.__file__}
    counts = result["sent"]
    def save():
        report.write_text(json.dumps(result, indent=2, default=str))
    try:
        if h._service_active():
            raise RuntimeError("Production bridge still running")
        cfg = load(config)
        wifi = Wifi(cfg)
        interface = cfg.interface
        def command(*args):
            proc = subprocess.run(args, capture_output=True, text=True, timeout=10)
            return {"command":list(args),"exit":proc.returncode,
                    "out":proc.stdout.strip(),"err":proc.stderr.strip()}
        def ensure_link():
            nonlocal original_mtu
            wifi.wait()
            route = command("ip","-j","route","get","172.31.1.1")
            if route["exit"] or json.loads(route["out"])[0].get("dev") != interface:
                wifi.connect()
                route = command("ip","-j","route","get","172.31.1.1")
            if route["exit"] or json.loads(route["out"])[0].get("dev") != interface:
                raise RuntimeError("No Cozmo route via configured adapter: "+str(route))
            current = int(Path("/sys/class/net",interface,"mtu").read_text())
            if original_mtu is None:
                original_mtu = current
            result["network_before"] = [
                command("ip","-d","link","show","dev",interface),
                command("ip","-4","route","get","172.31.1.1"),
                command("nmcli","-f","802-11-wireless.mtu,IP4,DHCP4","connection","show",wifi.profile)]
            if name.startswith("mtu1500-") and current != 1500:
                change = command("ip","link","set","dev",interface,"mtu","1500")
                result["mtu_change"] = change
                if change["exit"]:
                    raise RuntimeError(str(change))
            result["effective_mtu"] = int(Path("/sys/class/net",interface,"mtu").read_text())
            result["route"] = command("ip","-4","route","get","172.31.1.1")
            save()
        result["connection_attempts"] = []
        for attempt in range(1, 5):
            ensure_link()
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
        if mode=="idle":
            result["ping"] = [command("ping","-I",interface,"-c","2","-W","1","-M","do","-s",str(n),"172.31.1.1") for n in (548,549,600,762,1200,1472)]
        result["status"] = "RUNNING"
        if mode.startswith(("tone","speech")):
            cli.set_volume(12000)
            time.sleep(1)
        if name == "audio-tone-volume":
            cli.set_volume(12000)
            time.sleep(1)
        if name.startswith("audio-"):
            # A stream of duplicate ACKs should not prevent retransmission
            # of an outstanding command. Probe this in the worker only.
            sender = cli.conn.send_thread
            original_resend = sender._resend_messages
            last_retry = [time.perf_counter()]
            def retry_pending(self):
                now = time.perf_counter()
                with self.lock:
                    pending = self.window.get()
                if pending and now - last_retry[0] >= .15:
                    last_retry[0] = now
                    return pending
                return []
            sender._resend_messages = types.MethodType(retry_pending, sender)
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
        if mode.startswith("batch"):
            # Multiple individually valid, small display packets in one
            # reliable datagram, without exceeding the 62-packet window.
            # Choose one moderately sized image and fill with silence.
            target = int(mode[5:])
            import random
            rng = random.Random(321)
            img = Image.new("1",(128,32),0)
            pixels = img.load()
            for x in range(8):
                for y in range(32):
                    pixels[x,y] = 255 if rng.getrandbits(1) else 0
            pkt = p.DisplayImage(image=bytes(ImageEncoder(img).encode()))
            # Force grouping only for this test, using ordinary sequence
            # allocation and the normal frame serializer.
            sender = cli.conn.send_thread
            original = sender._send_packets
            group = [pkt] * max(1,(target-14)//(4+len(pkt)))
            remaining = target-14-sum(4+len(x) for x in group)
            group.extend(p.OutputSilence() for _ in range(max(0,remaining//4)))
            if len(group)>60:
                raise RuntimeError("Probe exceeds reliable window")
            buffered = []
            expected = len(group)
            def grouped(self, packets, last_ack):
                if len(buffered)<expected:
                    buffered.extend(packets)
                    if len(buffered)<expected:
                        return
                    self._send_packets = original
                    original(buffered, last_ack)
                else:
                    original(packets,last_ack)
            if before["pending_reliable_packets"] or before["queued_app_commands"]:
                raise RuntimeError("Probe requires empty send window")
            sender._send_packets = types.MethodType(grouped,sender)
            result["batch_requested_bytes"]=target
            result["batch_packets"]=expected
            for packet in group:
                send(packet)
        if mode.startswith("large"):
            # Valid binary image with controlled RLE complexity. Search for
            # a payload close to the requested size without padding it.
            import random
            target = int(mode[5:])
            best = None
            for columns in range(1,129):
                rng = random.Random(1234)
                img = Image.new("1",(128,32),0)
                pixels = img.load()
                for x in range(columns):
                    for y in range(32):
                        pixels[x,y] = 255 if rng.getrandbits(1) else 0
                pkt = p.DisplayImage(image=bytes(ImageEncoder(img).encode()))
                if best is None or abs(len(pkt)-target)<abs(len(best)-target):
                    best=pkt
            result["large_display_payload_bytes"]=len(best)
            send(best)
        if mode.endswith("_once") and mode.startswith("audio_"):
            audio_probe(mode.split("_")[1])
        # Precompute a full second of quiet original PCM tone. Use the
        # exact same waveform for both encoders; no disk or TTS dependencies.
        import struct
        tone_packets = []
        if mode.startswith("tone"):
            for index in range(30):
                pcm = [int(1800*math.sin(2*math.pi*440*(index*744+i)/22320))
                       for i in range(744)]
                if mode == "tone_stock":
                    payload = bytes(audio.bytes_to_cozmo(struct.pack("<744h",*pcm),1,1))
                else:
                    payload = bytes(ulaw(x) for x in pcm)
                tone_packets.append(p.OutputAudio(samples=payload))
            result["tone_encoding"] = mode
        message = "Hello! This is Cozmo. The speaker and display are working together."
        speech_packets = []
        if mode.startswith("speech"):
            # eSpeak NG is a local, deterministic test source; ffmpeg
            # only converts PCM format, not mu-law.
            wav = subprocess.run(["espeak-ng","--stdout","-s","145",message],
                                 capture_output=True,check=True,timeout=20).stdout
            pcm = subprocess.run(["ffmpeg","-hide_banner","-loglevel","error",
                                  "-i","pipe:0","-f","s16le","-ac","1","-ar","22320","pipe:1"],
                                 input=wav,capture_output=True,check=True,timeout=20).stdout
            pcm += bytes((-len(pcm)) % 1488)
            for offset in range(0,len(pcm),1488):
                samples = struct.unpack("<744h",pcm[offset:offset+1488])
                speech_packets.append(p.OutputAudio(samples=bytes(ulaw(int(x*.5)) for x in samples)))
            if not speech_packets:
                raise RuntimeError("Speech synthesis produced no audio")
            result["speech_frames"] = len(speech_packets)
        faces = []
        if mode in ("real_face","speech_face"):
            from .face import status_image
            for page in range(6):
                img = status_image(voltage=3.85, percent=65, charging=False,
                                   last_tts=message, scroll=page, voltage_only=True)
                payload = bytes(ImageEncoder(img).encode())
                if len(payload) + 20 > 1472:
                    raise RuntimeError("Rendered face exceeds one unfragmented UDP datagram")
                faces.append(payload)
            result["face_sizes"] = [len(x) for x in faces]
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
                if int(Path("/sys/class/net",interface,"mtu").read_text()) != 1500:
                    result["status"]="FAIL"
                    result["reason"]="Interface MTU changed during test"
                    break
                result["samples"].append({"t": round(now-start, 3), **m,
                    "animation_bytes_played": getattr(cli,"num_anim_bytes_played",None),
                    "animation_drop_count": getattr(cli,"client_drop_count",None)})
                next_sample = now + .25
                if len(result["samples"]) % 4 == 1:
                    save()
                if (now-last_rx > 3 or now-last_ack > 3 or
                    m["queued_app_commands"] > 12 or m["pending_reliable_packets"] >= 12):
                    result["status"] = "FAIL"
                    result["reason"] = "RX/ACK stalled or transport backlog exceeded limit"
                    break
            if rate and now >= next_frame:
                if mode.startswith("speech"):
                    cycle = len(speech_packets) + 120
                    phase = frame % cycle
                    if 30 <= phase < 30+len(speech_packets) and (mode=="speech_face" or frame<cycle):
                        # New packet instance: do not reuse sequence-bearing objects.
                        send(p.OutputAudio(samples=speech_packets[phase-30].samples))
                    else:
                        send(p.OutputSilence())
                elif mode.startswith("tone"):
                    phase = frame % 150
                    # One second of actual audio followed by four seconds
                    # of silence; short tests play only one burst.
                    if 30 <= phase < 60 and (mode=="tone_display" or frame<60):
                        send(tone_packets[phase-30])
                    else:
                        send(p.OutputSilence())
                elif mode.startswith("audio_") and frame == 30:
                    audio_probe(mode.split("_")[1])
                else:
                    send(p.OutputSilence())
                if faces and frame % 60 == 0:
                    send(p.DisplayImage(image=faces[(frame//60)%len(faces)]))
                if mode == "image_sparse" and frame % 30 == 0:
                    send(p.DisplayImage(image=encoded))
                if mode in ("patterns","tone_display") and frame % 30 == 0:
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
        try:
            if cli is not None:
                cli.stop()
        finally:
            if original_mtu is not None and interface and name.startswith("mtu1500-"):
                result["mtu_restore"] = command("ip","link","set","dev",interface,"mtu",str(original_mtu))
                save()
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
    import shutil
    if not shutil.which("espeak-ng") or not shutil.which("ffmpeg"):
        ap.error("Install test dependencies: sudo dnf install espeak-ng ffmpeg (or ffmpeg-free)")
    if not h._pycozmo_transport_patched():
        ap.error("Installed PyCozmo transport patch not detected")
    if not 5 <= args.duration <= 120:
        ap.error("duration must be 5..120 seconds")
    root = Path("/var/log/cozmo-roverd/animation-tests") / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root.mkdir(parents=True)
    from .config import load
    interface = load(args.config).interface
    active = h._service_active()
    restore_mtu = None
    results = {}
    try:
        if active:
            h._service_action("stop")
        if h._service_active():
            raise RuntimeError("Could not stop bridge")
        print("Waiting for previous bridge session to expire...", flush=True)
        time.sleep(10)
        for name, rate, mode, camera in CASES:
            deps = (["mtu1500-idle"] if name.startswith("mtu1500-") and name!="mtu1500-idle"
                    else ["idle-animation-state"] if name.startswith("original-") else [])
            if name=="mtu1500-speech-face":
                deps.extend(["mtu1500-speech","mtu1500-real-face"])
            if name=="mtu1500-speech-face-soak":
                deps.append("mtu1500-speech-face")

            blocked = [d for d in deps if results.get(d)!="PASS"]
            if blocked:
                results[name]="SKIP"
                print(f"{name}: SKIP prerequisites {blocked}",flush=True)
                continue
            d = root/name
            d.mkdir()
            restore_mtu = None
            if name.startswith("mtu1500-"):
                try:
                    restore_mtu = int(Path("/sys/class/net",interface,"mtu").read_text())
                except OSError:
                    pass
            print(f"Testing {name}...",flush=True)
            timedout=False
            case_duration = max(args.duration, 120) if name.endswith("-soak") else max(args.duration, 20)
            with (d/"console.log").open("w") as log:
                try:
                    proc=subprocess.run([sys.executable,"-m","cozmo_roverd.animation_tests","--worker",name,
                        "--directory",str(d),"--config",args.config,"--duration",str(case_duration)],
                        stdout=log,stderr=subprocess.STDOUT,timeout=case_duration+150)
                except subprocess.TimeoutExpired:
                    timedout=True
            if restore_mtu is not None:
                restore = subprocess.run(["ip","link","set","dev",interface,"mtu",str(restore_mtu)],capture_output=True,text=True)
                if restore.returncode:
                    print("WARNING: parent MTU restore failed:",restore.stderr,flush=True)
                else:
                    restore_mtu = None
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
                "network_before":r.get("network_before") if mode=="idle" or not r.get("before") else None,
                "effective_mtu":r.get("effective_mtu"),"route":r.get("route"),
                "mtu_change":r.get("mtu_change"),"mtu_restore":r.get("mtu_restore"),
                "ping":r.get("ping"),
                "speech_frames":r.get("speech_frames"),"face_sizes":r.get("face_sizes"),
                "sent":r.get("sent"),"rx_delta":b.get("received_frames",0)-a.get("received_frames",0),
                "audio_delta":b.get("audio_frames_reported_played",0)-a.get("audio_frames_reported_played",0),
                "max_pending":max((x["pending_reliable_packets"] for x in s),default=None),
                "max_queue":max((x["queued_app_commands"] for x in s),default=None),
                "wire_tx":sum(x.get("direction")=="tx" for x in wire),
                "wire_rx":sum(x.get("direction")=="rx" for x in wire),
                "audio_payload":r.get("audio_payload"),
                "large_display_payload_bytes":r.get("large_display_payload_bytes"),
                "batch_requested_bytes":r.get("batch_requested_bytes"),
                "batch_packets":r.get("batch_packets"),
                "large_wire":[x for x in wire if x.get("direction")=="tx" and x.get("bytes",0)>200][:2],
                "tx_problems":[x for x in wire if x.get("problem") and x.get("direction")=="tx"][:5],
                "audio_wire":[x for x in wire if x.get("direction")=="tx" and any(p.get("id")==142 for p in x.get("packets",[]))][:2],
                "audio_wire_count":sum(x.get("direction")=="tx" and any(p.get("id")==142 for p in x.get("packets",[])) for x in wire),
                "ack_tail":[{k:x.get(k) for k in ("kind","ack","first_seq","last_seq","bytes")} for x in wire if x.get("direction")=="rx"][-5:],
                "after":r.get("after"),
                "rx_problem_count":sum(bool(x.get("problem")) and x.get("direction")=="rx" for x in wire),
                "max_animation_drops":max((x.get("animation_drop_count") or 0 for x in s),default=None),
                "max_udp_tx_bytes":max((x.get("bytes",0) for x in wire if x.get("direction")=="tx"),default=0),
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
        if restore_mtu is not None:
            subprocess.run(["ip","link","set","dev",interface,"mtu",str(restore_mtu)],check=False)
        if active:
            h._service_action("start")
            print("Production bridge restarted.",flush=True)
    return int(any(v in ("FAIL","INCONCLUSIVE") for v in results.values()))

if __name__ == "__main__":
    sys.exit(main())
