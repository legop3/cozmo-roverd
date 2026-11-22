"""Feed latest PyCozmo PIL camera frames to ffmpeg/MediaMTX as low-latency H264."""
from __future__ import annotations
import logging
import math
import os
from pathlib import Path
import queue
import time
import subprocess
import threading
from PIL import Image, ImageDraw, ImageFont
LOG = logging.getLogger(__name__)

class Publisher:
    def __init__(self, url: str, fps: int = 15, hud: bool = True):
        self.url, self.fps = url, fps
        self.hud = hud
        self.state_provider = None
        self.frames: queue.Queue[Image.Image] = queue.Queue(maxsize=1)
        self.running = threading.Event()
        self.worker: threading.Thread | None = None
        self.process: subprocess.Popen | None = None
        self.encoder = None
        self.frame_count = 0
        self.dropped_frames = 0
        self.last_frame_time = None
        self.last_error = None
        self.device = None

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
        # Keep only the newest frame; conversion belongs to the publisher
        # worker, never the Cozmo packet processing thread.
        if not self.running.is_set(): return
        self.last_frame_time = time.monotonic()
        self.frame_count += 1
        if self.frames.full():
            try:
                self.frames.get_nowait()
                self.dropped_frames += 1
            except queue.Empty: pass
        try: self.frames.put_nowait(image)
        except queue.Full: self.dropped_frames += 1

    def diagnostics(self):
        return {
            "enabled": self.running.is_set(),
            "encoder": self.encoder,
            "hardware_device": self.device,
            "frames_received": self.frame_count,
            "frames_dropped": self.dropped_frames,
            "last_frame_age_sec": round(time.monotonic() - self.last_frame_time, 2) if self.last_frame_time else None,
            "publisher_running": self.process is not None and self.process.poll() is None,
            "last_error": self.last_error,
            "hud_enabled": self.hud,
        }
    @staticmethod
    def _format(value, precision=0, suffix=""):
        if value is None:
            return "--"
        if isinstance(value, bool):
            return "Y" if value else "N"
        try:
            return f"{float(value):.{precision}f}{suffix}"
        except (ValueError, TypeError):
            return "--"

    @staticmethod
    def imu_tilt(accel):
        """Return gravity-derived roll/pitch in degrees, or unknown.

        These angles are inferred from the instantaneous accelerometer;
        they are not a fused orientation solution and can wobble during motion.
        """
        if not isinstance(accel, dict):
            return None
        try:
            ax, ay, az = (float(accel[axis]) for axis in ("x", "y", "z"))
        except (TypeError, ValueError, KeyError):
            return None
        if not all(math.isfinite(v) for v in (ax, ay, az)):
            return None
        norm = math.sqrt(ax * ax + ay * ay + az * az)
        if norm < 0.001:
            return None
        roll = math.degrees(math.atan2(ay, az))
        pitch = math.degrees(math.atan2(-ax, math.hypot(ay, az)))
        return (roll, pitch)

    def render_frame(self, image, data=None):
        """Draw a compact top-center tilt compass on the existing frame."""
        output = image.convert("RGB")
        if output.size != (320, 240):
            output = output.resize((320, 240))
        if not self.hud:
            return output
        st = data or {}
        # A single lightweight Pillow drawing pass, executed by the encoder
        # thread after dequeue. No additional frame queue or encoding pass.
        draw = ImageDraw.Draw(output, "RGBA")
        cx, cy, radius = 160, 37, 26
        draw.rounded_rectangle((65, 2, 255, 100), radius=9,
                               fill=(9, 17, 23, 165),
                               outline=(210, 228, 233, 120), width=1)
        draw.ellipse((cx-radius, cy-radius, cx+radius, cy+radius),
                     fill=(17, 34, 43, 190), outline=(194, 222, 225, 225), width=1)
        draw.line((cx-radius+4, cy, cx+radius-4, cy),
                  fill=(125, 155, 166, 125), width=1)
        draw.line((cx, cy-radius+4, cx, cy+radius-4),
                  fill=(125, 155, 166, 125), width=1)
        draw.text((cx-3, cy-radius-10), "F", fill=(228, 243, 246, 255))
        tilt = self.imu_tilt(st.get("accel"))
        if tilt is not None:
            roll, pitch = tilt
            # Bubble position is a gravity tilt indicator, not planar
            # displacement. Clamp only the drawing, not the measurements.
            dx = max(-19, min(19, roll * 0.34))
            dy = max(-19, min(19, pitch * 0.34))
            scale = min(1.0, 19.0 / max(math.hypot(dx, dy), 1e-6))
            bx, by = int(round(cx + dx*scale)), int(round(cy + dy*scale))
            draw.ellipse((bx-4, by-4, bx+4, by+4), fill=(78, 239, 210, 255),
                         outline=(234, 255, 251, 255), width=1)
        else:
            roll = pitch = None
            draw.text((cx-8, cy-4), "IMU", fill=(255, 182, 103, 255))
        # F denotes robot-forward, not magnetic north. Heading is from
        # local wheel odometry rather than an absolute compass reference.
        # Heading from wheel odometry is separate from gravity tilt; indicate
        # the current robot yaw as a small amber pointer on the circle.
        heading = st.get("heading")
        if isinstance(heading, (float, int)) and math.isfinite(heading):
            theta = math.radians(heading)
            hx = int(round(cx + 21 * math.sin(theta)))
            hy = int(round(cy - 21 * math.cos(theta)))
            draw.ellipse((hx-2, hy-2, hx+2, hy+2), fill=(255, 193, 83, 255))
        # Peripheral stats stay in the same top-center module, flanking
        # the compass instead of occupying full-width top/bottom strips.
        pct = self._format(st.get("battery_pct"), 0, "%")
        draw.text((70, 13), f"BAT {pct}", fill=(173, 239, 222, 255))
        draw.text((70, 26), self._format(st.get("voltage"), 2, "V"),
                  fill=(226, 237, 240, 255))
        dock = "CHARGING" if st.get("charging") else "DOCKED" if st.get("docked") else ""
        if dock:
            draw.text((70, 39), dock, fill=(105, 240, 179, 255))
        draw.text((70, 56), "L " + self._format(st.get("left"), 0),
                  fill=(226, 237, 240, 255))
        draw.text((70, 69), "R " + self._format(st.get("right"), 0),
                  fill=(226, 237, 240, 255))
        draw.text((192, 13), "HEAD " + self._format(st.get("head"), 0),
                  fill=(226, 237, 240, 255))
        draw.text((192, 26), "LIFT " + self._format(st.get("lift"), 0),
                  fill=(226, 237, 240, 255))
        draw.text((192, 39), "CLIFF " + self._format(st.get("cliff")),
                  fill=(226, 237, 240, 255))
        gyro = st.get("gyro") or {}
        draw.text((192, 56), "GZ " + self._format(gyro.get("z"), 1),
                  fill=(226, 237, 240, 255))
        draw.text((cx-45, 69), f"R {self._format(roll, 0)} P {self._format(pitch, 0)}",
                  fill=(223, 241, 244, 255))
        draw.text((cx-45, 84), f"HDG {self._format(heading, 0)}",
                  fill=(156, 235, 218, 255))
        if not st.get("connected", False):
            draw.rounded_rectangle((108, 44, 212, 58), radius=3,
                                   fill=(117, 36, 27, 225))
            draw.text((113, 46), "RECONNECTING", fill=(255, 236, 223, 255))
        return output

    def _offline_frame(self):
        image = Image.new("RGB", (320, 240), (12, 17, 23))
        draw = ImageDraw.Draw(image)
        draw.text((105, 103), "NO LIVE CAMERA", fill=(255, 255, 255))
        draw.text((98, 121), "Waiting for frames", fill=(160, 174, 190))
        return image

    def _run(self):
        while self.running.is_set():
            command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin"]
            if self.device:
                command += ["-vaapi_device", self.device]
            command += ["-fflags", "nobuffer", "-use_wallclock_as_timestamps", "1", "-f", "rawvideo", "-pixel_format", "rgb24",
                        "-video_size", "320x240", "-framerate", str(self.fps), "-i", "pipe:0",
                        "-an"]
            if self.device:
                command += ["-vf", "format=nv12,hwupload"]
            if self.encoder == "libx264":
                command += ["-preset", "ultrafast", "-tune", "zerolatency", "-threads", "2"]
            command += ["-c:v", self.encoder, "-pix_fmt", "yuv420p"] if not self.device else ["-c:v", self.encoder]
            command += ["-g", str(self.fps), "-bf", "0", "-fps_mode", "passthrough",
                        "-flush_packets", "1", "-muxdelay", "0", "-muxpreload", "0",
                        "-f", "rtsp", "-rtsp_transport", "tcp", self.url]
            try:
                env = os.environ.copy()
                if self.device:
                    env.setdefault("LIBVA_DRIVER_NAME", "i965")
                self.process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                                stdout=subprocess.DEVNULL, env=env)
                while self.running.is_set() and self.process.poll() is None:
                    try:
                        image = self.frames.get(timeout=0.5)
                        is_offline = False
                    except queue.Empty:
                        # Keep RTSP published during a physical robot outage.
                        # One fresh status slate per second, not a buffered
                        # sequence of old camera frames.
                        if self.last_frame_time is not None and time.monotonic() - self.last_frame_time < 1.0:
                            continue
                        image = self._offline_frame()
                        is_offline = True
                        time.sleep(0.5)
                    try:
                        data = self.state_provider() if self.state_provider is not None else {}
                        frame = self.render_frame(image, data).tobytes()
                        self.process.stdin.write(frame)
                    except (BrokenPipeError, OSError, ValueError) as exc:
                        self.last_error = str(exc)[:180]
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
