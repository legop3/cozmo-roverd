"""Bounded, connection-local animation playback and reliable recovery."""
import logging
import hashlib
import math
import json
import os
import re
import select
import signal
import struct
import sys
import tempfile
from pathlib import Path
import subprocess
import threading
import time
import types
from collections import deque
from pycozmo.audio import u_law_encoding

LOG = logging.getLogger(__name__)

def install_recovery(cli):
    sender = cli.conn.send_thread
    sender.roverd_retries = 0
    head = None
    last = time.perf_counter()
    def retry(self):
        nonlocal head, last
        now = time.perf_counter()
        with self.lock:
            pending = self.window.get()
        current = pending[0][0] if pending else None
        if current != head or current is None:
            head, last = current, now
        if pending and now-last >= .1:
            last = now
            self.roverd_retries += 1
            return pending
        return []
    original = sender._send_packets
    def separate(self, packets, last_ack):
        for seq, packet in packets:
            original([(seq, packet)], last_ack)
    sender._resend_messages = types.MethodType(retry, sender)
    sender._send_packets = types.MethodType(separate, sender)

def ulaw(sample):
    # Cozmo expects PyCozmo's wire representation, not standard G.711.
    # Verified by the controlled hardware encoder A/B test.
    return u_law_encoding(sample) & 255

def synthesize(text, speed=1.0, gain_peak=9000, engine="espeak", voice="", pitch=0):
    with tempfile.TemporaryDirectory(prefix="cozmo-tts-") as tmp:
        path = str(Path(tmp)/"speech.wav")
        if engine == "espeak":
            args = ["espeak-ng","--stdout","-s",str(round(145*speed)),
                    "-p",str(int(pitch) if pitch else 50),"-v",voice or "en-us","--",text]
            wav = subprocess.run(args,capture_output=True,check=True,timeout=40).stdout
        elif engine == "flite":
            args = ["flite","-voice",voice or "slt","-t",text,"-o",path]
            subprocess.run(args,capture_output=True,check=True,timeout=40)
            wav = Path(path).read_bytes()
        elif engine == "google":
            subprocess.run([sys.executable,"-m","cozmo_roverd.chromegtts",
                "--text="+text,"--voice="+(voice or "tpf"),"--pitch="+str(pitch or 1),
                "--speed="+str(speed),"--output="+path],capture_output=True,check=True,timeout=40)
            wav = Path(path).read_bytes()
        else:
            raise ValueError("Unsupported TTS engine")
    pcm = subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-i","pipe:0",
                          "-f","s16le","-ac","1","-ar","22320","pipe:1"],
                         input=wav,capture_output=True,check=True,timeout=20).stdout
    pcm += bytes((-len(pcm))%1488)
    samples = struct.unpack("<%dh"%(len(pcm)//2),pcm)
    peak = max(map(abs,samples),default=0)
    scale = gain_peak/peak if peak else 0
    encoded = bytes(ulaw(round(x*scale)) for x in samples)
    return deque(encoded[i:i+744] for i in range(0,len(encoded),744))

def speech_chunks(text, limit=120):
    # Prefer sentence/clause boundaries, then whitespace, never truncate.
    remaining = text.strip()
    while remaining:
        if len(remaining) <= limit:
            yield remaining
            break
        prefix = remaining[:limit+1]
        boundaries = [m.end() for m in re.finditer(r"[.!?;,:](?=\s)", prefix)]
        cut = max(boundaries, default=0)
        if cut < 25:
            cut = prefix.rfind(" ")
        if cut <= 0:
            cut = limit
        yield remaining[:cut].strip()
        remaining = remaining[cut:].lstrip()


def synthesis_worker():
    # Dedicated process: neither synthesis nor Python encoding holds the
    # bridge's GIL. Child engines and FFmpeg inherit the reduced priority.
    os.nice(10)
    text,speed,gain,engine,voice,pitch = json.loads(sys.stdin.read())
    for chunk in speech_chunks(text):
        for frame in synthesize(chunk,speed,gain,engine,voice,pitch):
            sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()


class Animation:
    def __init__(self, robot, cli):
        self.robot, self.cli = robot, cli
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.audio = deque()
        self.request = None
        self.generation = 0
        self.horn = None
        self.horn_index = 0
        self.face = None
        self.frames = 0
        self.last_synthesis = None
        self.backoffs = 0
        self.synth_thread = threading.Thread(target=self.synth_loop,daemon=True,name="cozmo-synthesis")
        self.play_thread = threading.Thread(target=self.play_loop,daemon=True,name="cozmo-animation")
        self.wake = threading.Event()

    def start(self):
        self.synth_thread.start()
        self.play_thread.start()

    def speak(self,text,speed,engine,voice,pitch):
        with self.lock:
            self.generation += 1
            self.request = (self.generation,text,speed,self.robot.tts_gain,engine,voice,pitch)
            self.audio.clear()
            self.last_synthesis = None
            self.robot.last_tts_frames = 0
        self.wake.set()

    def synth_loop(self):
        while not self.stop.is_set():
            self.wake.wait(.2)
            self.wake.clear()
            with self.lock:
                req,self.request = self.request,None
            if req is None:
                continue
            generation,text,speed,gain_peak,engine,voice,pitch = req
            proc = None
            digest = hashlib.sha256()
            count = 0
            try:
                with tempfile.TemporaryFile() as errors:
                    proc = subprocess.Popen(
                        [sys.executable,"-m","cozmo_roverd.animation","--synth-worker"],
                        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=errors,
                        start_new_session=True)
                    proc.stdin.write(json.dumps([text,speed,gain_peak,engine,voice,pitch]).encode())
                    proc.stdin.close()
                    pending = bytearray()
                    eof = False
                    while not self.stop.is_set():
                        with self.lock:
                            if generation != self.generation:
                                break
                            room = 60-len(self.audio)
                            if room and len(pending)>=744:
                                frame = bytes(pending[:744])
                                del pending[:744]
                                self.audio.append(frame)
                                digest.update(frame)
                                count += 1
                                self.robot.last_tts_frames = count
                                self.last_synthesis = {"engine":engine,"voice":voice,
                                    "codec":"pycozmo","peak":gain_peak,
                                    "encoded_sha256":digest.hexdigest(),"frames":count,
                                    "streaming":True,"complete":False}
                                self.robot.last_tts_error = None
                                continue
                        if eof:
                            if pending:
                                raise RuntimeError("Incomplete audio frame")
                            if proc.wait(timeout=2):
                                errors.seek(0)
                                raise RuntimeError(errors.read().decode(errors="replace")[-180:])
                            with self.lock:
                                if generation == self.generation and self.last_synthesis:
                                    self.last_synthesis["complete"] = True
                            break
                        if room <= 0:
                            self.stop.wait(.02)
                            continue
                        if select.select([proc.stdout],[],[],.1)[0]:
                            data = os.read(proc.stdout.fileno(),744*min(room,8))
                            if data:
                                pending.extend(data)
                            else:
                                eof = True
            except Exception as exc:
                if generation == self.generation and not self.stop.is_set():
                    self.robot.last_tts_error = str(exc)[:180]
                LOG.warning("Speech synthesis failed: %s",exc)
            finally:
                if proc is not None:
                    if proc.poll() is None:
                        try:
                            os.killpg(proc.pid,signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    proc.wait()
                    if proc.stdout:
                        proc.stdout.close()

    def set_horn(self, waveform, freqs):
        with self.lock:
            self.horn = (waveform, tuple(freqs), time.monotonic()+2.0) if freqs else None
            self.horn_index = 0

    def horn_frame(self, now):
        if self.horn is None:
            return None
        waveform,freqs,deadline = self.horn
        if now >= deadline:
            self.horn = None
            return None
        peak = self.robot.horn_gain
        samples = []
        for i in range(744):
            t = (self.horn_index+i)/22320
            env = min(1.0,t/.02,max(0,deadline-now)/.06)
            v = sum(math.sin(2*math.pi*f*t) if waveform=="sine"
                    else 2*((f*t)%1)-1 for f in freqs)/len(freqs)
            samples.append(ulaw(round(peak*env*v)))
        self.horn_index += 744
        return bytes(samples)

    def play_loop(self):
        from pycozmo.protocol_encoder import OutputAudio, OutputSilence, DisplayImage
        sender = self.cli.conn.send_thread
        try:
            self.cli.set_volume(self.robot.speaker_volume)
            while not self.stop.is_set():
                start = time.monotonic()
                with self.robot.lock:
                    if self.robot.client is not self.cli or not self.robot.ready:
                        return
                    with sender.lock:
                        pending = len(sender.window.get())
                    if pending >= 4 or sender.queue.qsize() or start-self.robot._animation_control_at < .08:
                        self.backoffs += 1
                    else:
                        with self.lock:
                            sample = self.horn_frame(start)
                            is_horn = sample is not None
                            if sample is None:
                                sample = self.audio.popleft() if self.audio else None
                            face,self.face = self.face,None
                        if sample is not None:
                            self.cli.conn.send(OutputAudio(samples=sample))
                            if not is_horn:
                                self.robot.last_tts_played_at = time.monotonic()
                        elif face is not None:
                            self.cli.conn.send(OutputSilence())
                        if sample is not None or face is not None:
                            self.frames += 1
                        if face is not None:
                            self.cli.conn.send(DisplayImage(image=face))
                            if sample is None:
                                self.cli.conn.send(OutputSilence())
                            self.robot.face_frames_sent += 1
                self.stop.wait(max(0,1/30-(time.monotonic()-start)))
        except Exception as exc:
            if not self.stop.is_set():
                self.robot.last_tts_error = str(exc)[:180]
                LOG.exception("Animation sender stopped")

    def close(self):
        self.stop.set()
        self.wake.set()
        self.play_thread.join(timeout=1)
        self.synth_thread.join(timeout=1)


if __name__ == "__main__" and "--synth-worker" in sys.argv:
    synthesis_worker()
