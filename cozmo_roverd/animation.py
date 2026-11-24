"""Bounded, connection-local animation playback and reliable recovery."""
import logging
import math
import struct
import subprocess
import threading
import time
import types
from collections import deque

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
    sign = 128 if sample < 0 else 0
    mag = min(abs(sample),32635)+132
    exponent = max(0,mag.bit_length()-8)
    return (~(sign | (exponent<<4) | ((mag>>(exponent+3))&15)))&255

def synthesize(text, speed=1.0):
    wav = subprocess.run(["espeak-ng","--stdout","-s",str(round(145*speed)),"--",text],
                         capture_output=True,check=True,timeout=20).stdout
    pcm = subprocess.run(["ffmpeg","-hide_banner","-loglevel","error","-i","pipe:0",
                          "-f","s16le","-ac","1","-ar","22320","pipe:1"],
                         input=wav,capture_output=True,check=True,timeout=20).stdout
    pcm += bytes((-len(pcm))%1488)
    return deque(bytes(ulaw(int(x*.1)) for x in struct.unpack("<744h",pcm[i:i+1488]))
                 for i in range(0,len(pcm),1488))

class Animation:
    def __init__(self, robot, cli):
        self.robot, self.cli = robot, cli
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.audio = deque()
        self.request = None
        self.generation = 0
        self.face = None
        self.frames = 0
        self.backoffs = 0
        self.synth_thread = threading.Thread(target=self.synth_loop,daemon=True,name="cozmo-synthesis")
        self.play_thread = threading.Thread(target=self.play_loop,daemon=True,name="cozmo-animation")
        self.wake = threading.Event()

    def start(self):
        self.synth_thread.start()
        self.play_thread.start()

    def speak(self,text,speed):
        with self.lock:
            self.generation += 1
            self.request = (self.generation,text,speed)
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
            generation,text,speed = req
            try:
                frames = synthesize(text,speed)
                with self.lock:
                    if generation == self.generation and not self.stop.is_set():
                        self.audio = frames
                        self.robot.last_tts_frames = len(frames)
                        self.robot.last_tts_error = None
            except Exception as exc:
                if generation == self.generation and not self.stop.is_set():
                    self.robot.last_tts_error = str(exc)[:180]
                LOG.warning("Speech synthesis failed: %s",exc)

    def play_loop(self):
        from pycozmo.protocol_encoder import OutputAudio, OutputSilence, DisplayImage
        sender = self.cli.conn.send_thread
        try:
            self.cli.set_volume(500)
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
                            sample = self.audio.popleft() if self.audio else None
                            face,self.face = self.face,None
                        if sample is not None:
                            self.cli.conn.send(OutputAudio(samples=sample))
                            self.robot.last_tts_played_at = time.monotonic()
                        else:
                            self.cli.conn.send(OutputSilence())
                        self.frames += 1
                        if face is not None:
                            self.cli.conn.send(DisplayImage(image=face))
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
