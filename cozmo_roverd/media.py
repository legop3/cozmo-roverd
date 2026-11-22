"""Feed latest PyCozmo PIL camera frames to ffmpeg/MediaMTX as low-latency H264."""
from __future__ import annotations
import logging
import os
from pathlib import Path
import queue
import time
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
        self.encoder = None

    def _test_encoder(self, encoder, *, device=None):
        # A listed FFmpeg encoder is not necessarily usable. Test a single
        # synthetic frame locally before declaring it ready.
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        if device:
            command += ["-vaapi_device", device]
        command += ["-f", "lavfi", "-i", "color=c=black:s=320x240:r=5", "-frames:v", "1"]
        if device:
            command += ["-vf", "format=nv12,hwupload"]
        command += ["-c:v", encoder, "-f", "null", "-"]
        env = os.environ.copy()
        if device:
            # 4th-gen Intel HD 4600 uses i965, not the newer iHD driver.
            env.setdefault("LIBVA_DRIVER_NAME", "i965")
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=12, check=False, env=env)
            if result.returncode:
                LOG.info("Encoder %s test failed: %s", encoder,
                         result.stderr.strip()[-400:])
                return False
            return True
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.warning("Encoder %s test could not run: %s", encoder, exc)
            return False

    def start(self):
        probe = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                               capture_output=True, text=True, check=True)
        codecs = probe.stdout
        self.encoder, self.device = None, None
        render = Path("/dev/dri")
        # The Cozmo camera is only 320x240. Software H.264 is cheap and
        # avoids Haswell VA-API failures when i965 cannot initialize i915.
        for encoder in ("libx264", "libopenh264"):
            if encoder in codecs and self._test_encoder(encoder):
                self.encoder = encoder
                break
        if self.encoder is None and "h264_vaapi" in codecs and render.is_dir():
            for device in sorted(render.glob("renderD*")):
                if self._test_encoder("h264_vaapi", device=str(device)):
                    self.encoder, self.device = "h264_vaapi", str(device)
                    break
        if self.encoder is None:
            raise RuntimeError("No working H.264 encoder. Check FFmpeg and VA-API drivers.")
        LOG.info("Video encoder selected: %s%s", self.encoder,
                 " on " + self.device if self.device else "")
        self.running.set()
        self.worker = threading.Thread(target=self._run, name="rtsp-publisher", daemon=True)
        self.worker.start()

    def submit(self, image: Image.Image):
        if not self.running.is_set():
            return
        frame = image.convert("RGB").resize((320, 240)).tobytes()
        if self.frames.full():
            try: self.frames.get_nowait()
            except queue.Empty: pass
        try: self.frames.put_nowait(frame)
        except queue.Full: pass

    def _run(self):
        while self.running.is_set():
            command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
            if self.device:
                command += ["-vaapi_device", self.device]
            command += ["-fflags", "nobuffer", "-f", "rawvideo", "-pixel_format", "rgb24",
                        "-video_size", "320x240", "-framerate", str(self.fps), "-i", "pipe:0",
                        "-an"]
            if self.device:
                command += ["-vf", "format=nv12,hwupload"]
            command += ["-c:v", self.encoder, "-pix_fmt", "yuv420p"] if not self.device else ["-c:v", self.encoder]
            command += ["-g", str(self.fps), "-bf", "0",
                        "-f", "rtsp", "-rtsp_transport", "tcp", self.url]
            try:
                env = os.environ.copy()
                if self.device:
                    env.setdefault("LIBVA_DRIVER_NAME", "i965")
                self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                                stdout=subprocess.DEVNULL, env=env)
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
            # Retry the publisher, but remain responsive to shutdown.
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
