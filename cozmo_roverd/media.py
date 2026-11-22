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
        self._ffmpeg_log_file = None

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

    @staticmethod
    def lift_fraction(height_mm):
        """Normalize Cozmo's reported lift height to its documented travel.

        The state provider reports lift height in millimeters (not arm angle).
        An icon can depict approximate physical orientation without pretending
        that it is a degree measurement.
        """
        try:
            height = float(height_mm)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(height):
            return None
        return max(0.0, min(1.0, (height - 32.0) / (92.0 - 32.0)))

    @staticmethod
    def _draw_lift_indicator(draw, height_mm):
        """Compact side-profile hinged arm beside the IMU bubble."""
        cx, cy, radius = 188, 17, 12
        bg = (9, 18, 24)
        edge = (216, 236, 239)
        muted = (80, 112, 125)
        active = (83, 241, 204)
        draw.ellipse((cx-radius-1, cy-radius-1, cx+radius+1, cy+radius+1),
                     fill=bg, outline=edge, width=1)
        # Base silhouette and pivot make this read as a moving lift arm,
        # not a second IMU ball.
        pivot_x, pivot_y = cx-6, cy+6
        draw.line((pivot_x-3, pivot_y+2, pivot_x+6, pivot_y+2),
                  fill=muted, width=2)
        fraction = Publisher.lift_fraction(height_mm)
        if fraction is None:
            draw.line((pivot_x, pivot_y, cx+6, cy-1),
                      fill=muted, width=2)
            draw.point((pivot_x, pivot_y), fill=edge)
            return
        # At low lift the arm points almost horizontally; at high lift
        # it rises toward vertical. This is a proportional position icon.
        theta = math.radians(12.0 + 65.0*fraction)
        length = 15.0
        endpoint_x = int(round(pivot_x + length*math.cos(theta)))
        endpoint_y = int(round(pivot_y - length*math.sin(theta)))
        draw.line((pivot_x, pivot_y, endpoint_x, endpoint_y),
                  fill=active, width=3)
        draw.ellipse((pivot_x-2, pivot_y-2, pivot_x+2, pivot_y+2),
                     fill=edge)
        draw.ellipse((endpoint_x-2, endpoint_y-2, endpoint_x+2, endpoint_y+2),
                     fill=active)

    def render_frame(self, image, data=None):
        """Small top-center IMU bubble with adjacent lift-position icon."""
        output = image.convert("RGB")
        if output.size != (320, 240):
            output = output.resize((320, 240))
        if not self.hud:
            return output
        st = data or {}
        draw = ImageDraw.Draw(output)
        cx, cy, radius = 160, 17, 13
        # Thin, compact round horizon indicator, approx. 28x28 pixels.
        # Draw directly on the current frame without another buffer.
        draw.ellipse((cx-radius-1, cy-radius-1, cx+radius+1, cy+radius+1),
                     fill=(9, 18, 24), outline=(216, 236, 239), width=1)
        draw.line((cx-9, cy, cx+9, cy), fill=(80, 112, 125), width=1)
        draw.line((cx, cy-9, cx, cy+9), fill=(80, 112, 125), width=1)
        tilt = self.imu_tilt(st.get("accel"))
        if tilt is not None:
            roll, pitch = tilt
            dx = max(-9, min(9, roll * 0.14))
            dy = max(-9, min(9, pitch * 0.14))
            norm = math.hypot(dx, dy)
            if norm > 9:
                dx, dy = dx * 9 / norm, dy * 9 / norm
            bx, by = int(round(cx + dx)), int(round(cy + dy))
            draw.ellipse((bx-3, by-3, bx+3, by+3),
                         fill=(83, 241, 204), outline=(233, 255, 248), width=1)
        else:
            draw.ellipse((cx-2, cy-2, cx+2, cy+2), fill=(180, 184, 190))
        self._draw_lift_indicator(draw, st.get("lift"))
        return output

    @staticmethod
    def _recovery_status(data, *, had_video=False):
        """Translate the current hardware supervisor stage to a concise step."""
        stage = data.get("hardware_stage", "starting")
        online = data.get("connected", False)
        if online or stage == "online":
            return ("CAMERA RECONNECTING" if had_video else "WAITING FOR CAMERA",
                    "Waiting for camera frames")
        title = "COZMO RECONNECTING" if had_video else "CONNECTING TO COZMO"
        steps = {
            "starting": "Starting connection",
            "connecting_wifi": "Connecting to robot Wi-Fi",
            "connecting_robot": "Connecting to robot",
            "cleaning_up": "Closing old connection",
            "retrying": "Waiting to retry",
        }
        return title, steps.get(stage, "Connecting to robot")

    def _offline_frame(self, data=None):
        """Show what reconnect is actually doing, keeping RTSP alive."""
        st = data or {}
        image = Image.new("RGB", (320, 240), (12, 17, 23))
        draw = ImageDraw.Draw(image)
        title, step = self._recovery_status(st, had_video=self.frame_count > 0)
        dots = "." * (1 + int(time.monotonic() * 1.5) % 3)
        # Keep only two centered lines: reconnection and its live stage.
        # No MRR information, counters, panels, or error dumps.
        for message, y, color in (
            (title + dots, 105, (235, 243, 245)),
            (step, 128, (160, 185, 196)),
        ):
            bbox = draw.textbbox((0, 0), message)
            width = bbox[2] - bbox[0]
            draw.text(((320 - width) // 2, y - bbox[1]),
                      message, fill=color)
        return image

    def _run(self):
        while self.running.is_set():
            command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
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
                logpath = Path("/run/cozmo-roverd/ffmpeg.log")
                logpath.parent.mkdir(parents=True, exist_ok=True)
                # Each retry overwrites the preceding FFmpeg errors. The
                # service journal stays readable, and local diagnostics
                # retain the most recent encoder/RTSP failure.
                self._ffmpeg_log_file = logpath.open("wb")
                process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                           stdout=subprocess.DEVNULL,
                                           stderr=self._ffmpeg_log_file, env=env)
                self.process = process
                # Keep this worker's own process reference: stop() may clear
                # self.process concurrently during service shutdown.
                while self.running.is_set() and process.poll() is None:
                    try:
                        image = self.frames.get(timeout=0.5)
                        # A buffered image must not masquerade as live video
                        # after the supervisor has dropped the Cozmo link.
                        got_frame = True
                    except queue.Empty:
                        got_frame = False
                        image = None
                    try:
                        data = self.state_provider() if self.state_provider is not None else {}
                        fresh = (self.last_frame_time is not None and
                                 time.monotonic() - self.last_frame_time < 1.0)
                        # The camera can pause independently while the robot
                        # stays online; show that distinction explicitly.
                        if got_frame and fresh and data.get("connected", True):
                            frame = self.render_frame(image, data).tobytes()
                        elif not fresh:
                            frame = self._offline_frame(data).tobytes()
                            time.sleep(0.5)
                        else:
                            # Waiting for the next frame within the normal
                            # one-second camera gap; avoid fake offline flashes.
                            continue
                        process.stdin.write(frame)
                    except (BrokenPipeError, OSError, ValueError) as exc:
                        self.last_error = str(exc)[:180]
                        break
            except (OSError, ValueError) as exc:
                LOG.warning("Cannot start video publisher: %s", exc)
            finally:
                self._terminate()
                self._ffmpeg_log_file = None
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
        if self._ffmpeg_log_file is not None:
            self._ffmpeg_log_file.close()
            self._ffmpeg_log_file = None
        if p.returncode and self.running.is_set():
            self.last_error = ("FFmpeg exited with status "
                               + str(p.returncode) + "; see /run/cozmo-roverd/ffmpeg.log")

    def stop(self):
        self.running.clear()
        self._terminate()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=3)
