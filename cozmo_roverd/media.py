"""Feed latest PyCozmo PIL camera frames to ffmpeg/MediaMTX as low-latency H264."""
from __future__ import annotations
import logging
import queue
import subprocess
import threading
from PIL import Image
LOG = logging.getLogger(__name__)

class Publisher:
    def __init__(self, url: str, fps: int = 15):
        self.url, self.fps = url, fps
        self.frames: queue.Queue[bytes] = queue.Queue(maxsize=2)
        self.running = threading.Event()
        self.worker: threading.Thread | None = None
        self.process: subprocess.Popen | None = None
        self.last_frame = None

    def start(self):
        self.running.set()
        self.worker = threading.Thread(target=self._run, name="rtsp-publisher", daemon=True)
        self.worker.start()

    def submit(self, image: Image.Image):
        if not self.running.is_set():
            return
        frame = image.convert("rgb").resize((320, 240)).tobytes()
        if self.frames.full():
            try: self.frames.get_nowait()
            except queue.Empty: pass
        try: self.frames.put_nowait(frame)
        except queue.Full: pass

    def _run(self):
        while self.running.is_set():
            command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
                       "-fflags", "nobuffer", "-f", "rawvideo", "-pixel_format", "rgb24",
                       "-video_size", "320x240", "-framerate", str(self.fps),
                       "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "ultrafast",
                       "-tune", "zerolatency", "-pix_fmt", "yuv420p", "-g", str(self.fps),
                       "-bf", "0", "-f", "rtsp", "-rtsp_transport", "tcp", self.url]
            try:
                self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                                stdout=subprocess.DEVNULL)
                while self.running.is_set() and self.process.poll() is None:
                    try: frame = self.frames.get(timeout=0.5)
                    except queue.Empty: continue
                    try:
                        self.process.stdin.write(frame)
                        self.process.stdin.flush()
                    except (BrokenPipeError, OSError):
                        break
            except (OSError, ValueError) as exc:
                LOG.warning("Cannot start video publisher: %s", exc)
            finally:
                self._terminate()
            if self.running.wait(0.0):
                # Sleep interruptibly without blocking stop.
                import time
                for _ in range(30):
                    if not self.running.is_set(): break
                    time.sleep(0.1)

    def _terminate(self):
        p = self.process
        self.process = None
        if p is None: return
        if p.stdin:
            try: p.stdin.close()
            except OSError: pass
        if p.poll() is None:
            p.terminate()
        try: p.wait(timeout=2)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()

    def stop(self):
        self.running.clear()
        self._terminate()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=3)
