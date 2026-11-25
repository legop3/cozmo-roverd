"""Bounded, connection-local animation playback and reliable recovery."""
import logging
import hashlib
import math
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
            try:
                frames = synthesize(text,speed,gain_peak,engine,voice,pitch)
                with self.lock:
                    if generation == self.generation and not self.stop.is_set():
                        self.last_synthesis = {"engine":engine,"voice":voice,"codec":"pycozmo","peak":gain_peak,
                                               "encoded_sha256":hashlib.sha256(b"".join(frames)).hexdigest(),
                                               "frames":len(frames)}
                        LOG.info("TTS encoded: %s",self.last_synthesis)
                        self.audio = frames
                        self.robot.last_tts_frames = len(frames)
                        self.robot.last_tts_error = None
            except Exception as exc:
                if generation == self.generation and not self.stop.is_set():
                    self.robot.last_tts_error = (exc.stderr.decode(errors="replace").strip() if isinstance(exc,subprocess.CalledProcessError) and exc.stderr else str(exc))[:180]
                LOG.warning("Speech synthesis failed: %s",exc)

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
