"""Camera frame path regression tests (no Cozmo or RTSP server needed)."""
from PIL import Image
from cozmo_roverd.media import Publisher


def test_color_frame_converts_to_ffmpeg_rgb24():
    publisher = Publisher("rtsp://localhost:8554/cozmo")
    publisher.running.set()
    publisher.submit(Image.new("RGB", (320, 240), (10, 20, 30)))
    data = publisher.frames.get_nowait().convert("RGB").tobytes()
    assert len(data) == 320 * 240 * 3
    assert data[:3] == bytes((10, 20, 30))


def test_gray_frame_also_supported():
    publisher = Publisher("rtsp://localhost:8554/cozmo")
    publisher.running.set()
    publisher.submit(Image.new("L", (320, 240), 128))
    data = publisher.frames.get_nowait().convert("RGB").tobytes()
    assert len(data) == 320 * 240 * 3
    assert data[:3] == bytes((128, 128, 128))


def test_latest_frame_wins_when_encoder_behind():
    publisher = Publisher("rtsp://localhost:8554/cozmo")
    publisher.running.set()
    for color in ((1, 2, 3), (4, 5, 6), (7, 8, 9)):
        publisher.submit(Image.new("RGB", (320, 240), color))
    assert publisher.frames.qsize() == 1
    assert publisher.frames.get_nowait().getpixel((0, 0)) == (7, 8, 9)
    assert publisher.dropped_frames == 2
    assert publisher.diagnostics()["frames_received"] == 3


def test_hud_renders_telemetry_without_changing_frame_size():
    publisher = Publisher("rtsp://localhost:8554/cozmo", hud=True)
    frame = Image.new("RGB", (320, 240), (30, 50, 80))
    result = publisher.render_frame(frame, {
        "connected": True, "battery_pct": 67, "voltage": 3.94,
        "charging": False, "docked": False, "head": 20,
        "lift": 48, "cliff": True, "left": 100, "right": -100,
        "heading": 45, "accel": {"x": 0, "y": 0, "z": 9.81},
    })
    assert result.size == (320, 240)
    assert result.mode == "RGB"
    assert result.getpixel((160, 17)) != frame.getpixel((160, 17))
    # No text panels: the picture is unchanged outside a 30px IMU ball.
    assert result.getpixel((160, 120)) == (30, 50, 80)
    assert result.getpixel((20, 8)) == (30, 50, 80)
    assert frame.getpixel((160, 17)) == (30, 50, 80)


def test_hud_can_be_disabled_without_crop():
    publisher = Publisher("rtsp://localhost:8554/cozmo", hud=False)
    frame = Image.new("RGB", (320, 240), (10, 20, 30))
    assert publisher.render_frame(frame, {"connected": True}).tobytes() == frame.tobytes()


def test_offline_slate_is_full_frame():
    publisher = Publisher("rtsp://localhost:8554/cozmo")
    frame = publisher._offline_frame()
    assert frame.size == (320, 240)
    assert frame.mode == "RGB"


def test_imu_tilt_compass_uses_acceleration():
    publisher = Publisher("rtsp://localhost:8554/cozmo")
    neutral = publisher.imu_tilt({"x": 0, "y": 0, "z": 9.81})
    tipped = publisher.imu_tilt({"x": 0, "y": 9.81, "z": 0})
    assert neutral == (0.0, -0.0)
    assert round(tipped[0]) == 90
    assert publisher.imu_tilt({"x": 0, "y": 0, "z": 0}) is None
    frame = Image.new("RGB", (320, 240), (30, 50, 80))
    a = publisher.render_frame(frame, {"connected": True,
            "accel": {"x": 0, "y": 0, "z": 9.81}})
    b = publisher.render_frame(frame, {"connected": True,
            "accel": {"x": 0, "y": 9.81, "z": 0}})
    assert a.tobytes() != b.tobytes()
    assert a.getpixel((20, 220)) == (30, 50, 80)
