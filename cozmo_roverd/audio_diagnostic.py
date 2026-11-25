"""Short, quiet, evidence-producing audio comparison; no production edits."""
import argparse
import hashlib
import json
import math
import struct
import subprocess
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from . import hardware_tests as h
from .animation import install_recovery

RATE = 22320
PEAK = 9000
VOLUME = 500
TEXT = "Test test."

def sha(data):
    return hashlib.sha256(data).hexdigest()

def ffmpeg(data, *args):
    return subprocess.run(["ffmpeg","-hide_banner","-loglevel","error",*args],
        input=data,capture_output=True,check=True,timeout=20).stdout

def metrics(data):
    values = struct.unpack("<%dh"%(len(data)//2),data)
    return {"peak":max(map(abs,values),default=0),
            "rms":round(math.sqrt(sum(x*x for x in values)/max(1,len(values))),2),
            "clipped_samples":sum(abs(x)>=32767 for x in values)}

def prepare(root):
    from pycozmo.audio import u_law_encoding
    wav = subprocess.run(["espeak-ng","--stdout","-s","145",TEXT],
        capture_output=True,check=True,timeout=20).stdout
    raw = ffmpeg(wav,"-i","pipe:0","-f","s16le","-ac","1","-ar",str(RATE),"pipe:1")
    # At most 1.2 seconds per audible test, with a short fade at the end.
    raw = raw[:36*1488]
    values = struct.unpack("<%dh"%(len(raw)//2),raw)
    peak = max(map(abs,values))
    scaled = [round(x*PEAK/peak*min(1,(len(values)-i)/447)) for i,x in enumerate(values)]
    pcm = struct.pack("<%dh"%len(scaled),*scaled)
    pcm += bytes((-len(pcm))%1488)
    standard = ffmpeg(pcm,"-f","s16le","-ar",str(RATE),"-ac","1","-i","pipe:0","-f","mulaw","pipe:1")
    stock = bytes(u_law_encoding(x)&255 for x in struct.unpack("<%dh"%(len(pcm)//2),pcm))
    decoded = ffmpeg(standard,"-f","mulaw","-ar",str(RATE),"-ac","1","-i","pipe:0","-f","s16le","pipe:1")
    (root/"source.pcm").write_bytes(pcm)
    (root/"standard.bin").write_bytes(standard)
    (root/"stock.bin").write_bytes(stock)
    (root/"decoded.pcm").write_bytes(decoded)
    print(json.dumps({"offline":{"source":metrics(raw),"scaled":metrics(pcm),
        "standard_decoded":metrics(decoded),"standard_sha":sha(standard),
        "stock_sha":sha(stock),"frames":len(standard)//744,
        "different_bytes":sum(a!=b for a,b in zip(standard,stock))}}),flush=True)
    return standard,stock

def worker(root,config,volume):
    import pycozmo
    from pycozmo import protocol_encoder as p
    from pycozmo.frame import Frame
    from .config import load
    from .wifi import Wifi
    standard,stock = prepare(root)
    cfg=load(config)
    cli=None
    for attempt in range(4):
        Wifi(cfg).wait()
        cli=pycozmo.Client(enable_animations=False,enable_procedural_face=False)
        install_recovery(cli)
        try:
            cli.start()
            cli.connect()
            cli.wait_for_robot(timeout=12)
            break
        except Exception:
            cli.stop()
            cli=None
            if attempt==3: raise
            time.sleep(5)
    sender=cli.conn.send_thread
    trace=[]
    original=sender._send_raw_frame
    def tap(self,raw):
        result=original(raw)
        try:
            frame=Frame.from_bytes(raw)
            for pkt in frame.pkts:
                if isinstance(pkt,(p.OutputAudio,p.OutputSilence,p.SetRobotVolume)):
                    trace.append({"t":time.monotonic(),"seq":frame.first_seq,
                        "kind":type(pkt).__name__,"payload":pkt.to_bytes().hex()})
        except Exception as exc:
            trace.append({"error":str(exc)})
        return result
    sender._send_raw_frame=types.MethodType(tap,sender)
    def snap():
        return {**h._snapshot(cli),"bytes_played":cli.num_anim_bytes_played,
                "drops":cli.client_drop_count}
    def drain():
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            s=snap()
            if not s["pending_reliable_packets"] and not s["queued_app_commands"]:
                return
            time.sleep(.01)
        raise RuntimeError("Reliable queue did not drain")
    def trial(name,data,volume,continuous):
        cli.set_volume(volume)
        drain()
        time.sleep(.25)
        before=snap()
        offset=len(trace)
        samples=[]
        start=time.monotonic()
        deadline=start
        packets=[p.OutputAudio(samples=data[i:i+744]) for i in range(0,len(data),744)]
        if continuous:
            packets=[p.OutputSilence() for _ in range(9)]+packets+[p.OutputSilence() for _ in range(12)]
        for pkt in packets:
            now=time.monotonic()
            if now<deadline: time.sleep(deadline-now)
            s=snap()
            if s["pending_reliable_packets"]>=4 or s["queued_app_commands"]>=4:
                drain()
            cli.conn.send(pkt)
            samples.append({"t":time.monotonic()-start,**snap()})
            deadline=max(deadline+1/30,time.monotonic()+.001)
        drain()
        time.sleep(.5)
        after=snap()
        cli.set_volume(0)
        drain()
        records=trace[offset:]
        first={}
        for row in records:
            if row.get("kind")=="OutputAudio":
                first.setdefault(row["seq"],row)
        audio=list(first.values())
        observed=b"".join(bytes.fromhex(row["payload"]) for row in audio)
        intervals=[round((b["t"]-a["t"])*1000,2) for a,b in zip(audio,audio[1:])]
        result={"case":name,"volume_command":volume,"clocked":continuous,
            "expected_audio_frames":len(data)//744,"wire_unique_audio":len(audio),
            "wire_audio_matches":observed==data,"wire_sha":sha(observed),
            "wire_repeated_audio":sum(x.get("kind")=="OutputAudio" for x in records)-len(audio),
            "volume_packets":[int.from_bytes(bytes.fromhex(x["payload"]),"little") for x in trace if x.get("kind")=="SetRobotVolume"][-2:],
            "played_delta":after["audio_frames_reported_played"]-before["audio_frames_reported_played"],
            "expected_clock_frames":len(packets),"bytes_played_delta":after["bytes_played"]-before["bytes_played"],
            "drop_delta":(after["drops"]-before["drops"])%256,
            "max_pending":max(s["pending_reliable_packets"] for s in samples),
            "wire_gap_ms":{"min":min(intervals,default=None),"max":max(intervals,default=None),
                           "over_45ms":sum(x>45 for x in intervals)},
            "pending_after":after["pending_reliable_packets"]}
        (root/(name+".json")).write_text(json.dumps({"summary":result,"samples":samples,"wire":records},indent=2))
        print(json.dumps(result),flush=True)
        if not result["wire_audio_matches"] or result["drop_delta"] or result["pending_after"]:
            raise RuntimeError("Transport or payload discrepancy; skipping remaining playback")
    try:
        cli.set_volume(0)
        drain()
        cli.enable_camera(enable=True,color=True)
        time.sleep(1)
        trial("muted-standard",standard,0,True)
        print(f"Silent check complete. Two short speech samples follow at volume {volume}; Ctrl-C stops the test.",flush=True)
        for name,data,continuous in [("standard-clocked",standard,True),("stock-clocked",stock,True)]:
            print("Playing "+name,flush=True)
            trial(name,data,volume,continuous)
            time.sleep(1)
    finally:
        try:
            cli.set_volume(0)
            time.sleep(.15)
        finally:
            cli.stop()
            (root/"wire.json").write_text(json.dumps(trace))

def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config",default="/etc/cozmo-roverd.toml")
    ap.add_argument("--worker")
    ap.add_argument("--volume",type=int,default=VOLUME,help="Fixed hardware volume for both samples; no automatic increase")
    args=ap.parse_args()
    if not 0 <= args.volume <= 65535:
        ap.error("volume must be 0..65535")
    if args.worker:
        worker(Path(args.worker),args.config,args.volume)
        return
    root=Path("/var/log/cozmo-roverd/audio-diagnostic")/datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    root.mkdir(parents=True)
    active=h._service_active()
    try:
        if active: h._service_action("stop")
        if h._service_active(): raise RuntimeError("Bridge still active")
        time.sleep(10)
        proc=subprocess.Popen([sys.executable,"-m","cozmo_roverd.audio_diagnostic","--worker",str(root),"--config",args.config,"--volume",str(args.volume)])
        try:
            rc=proc.wait(timeout=160)
            if rc: raise RuntimeError("Diagnostic worker exit "+str(rc))
        except BaseException:
            if proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            raise
    finally:
        print("Evidence: "+str(root),flush=True)
        if active:
            h._service_action("start")
            print("Production bridge restarted.",flush=True)

if __name__=="__main__":
    main()
