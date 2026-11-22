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
    # No text panels: the picture is unchanged outside the two tiny gauges.
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


def test_lift_gauge_tracks_height_without_claiming_measured_angle():
    publisher = Publisher("rtsp://localhost:8554/cozmo", hud=True)
    base = Image.new("RGB", (320, 240), (30, 50, 80))
    low = publisher.render_frame(base, {"lift": 32, "accel": None})
    middle = publisher.render_frame(base, {"lift": 62, "accel": None})
    high = publisher.render_frame(base, {"lift": 92, "accel": None})
    assert low.crop((175, 3, 202, 31)).tobytes() != (
        middle.crop((175, 3, 202, 31)).tobytes())
    assert middle.crop((175, 3, 202, 31)).tobytes() != (
        high.crop((175, 3, 202, 31)).tobytes())
    # The original IMU bubble does not change as the lift moves.
    assert low.crop((145, 2, 175, 32)).tobytes() == (
        high.crop((145, 2, 175, 32)).tobytes())
    # Draw only within the compact two-indicator footprint.
    assert high.getpixel((210, 17)) == (30, 50, 80)
    assert high.getpixel((160, 100)) == (30, 50, 80)
    assert base.getpixel((188, 17)) == (30, 50, 80)


def test_lift_gauge_handles_missing_and_out_of_range_telemetry():
    import math
    publisher = Publisher("rtsp://localhost:8554/cozmo", hud=True)
    assert publisher.lift_fraction(None) is None
    assert publisher.lift_fraction(float("nan")) is None
    assert publisher.lift_fraction(float("inf")) is None
    assert publisher.lift_fraction("bad") is None
    assert publisher.lift_fraction(20) == 0.0
    assert publisher.lift_fraction(32) == 0.0
    assert publisher.lift_fraction(62) == 0.5
    assert publisher.lift_fraction(92) == 1.0
    assert publisher.lift_fraction(120) == 1.0
    frame = Image.new("RGB", (320, 240), (30, 50, 80))
    result = publisher.render_frame(frame, {"lift": None})
    assert result.size == (320, 240)
    assert result.getpixel((188, 17)) != frame.getpixel((188, 17))


def test_recovery_titles_reflect_actual_robot_supervisor_stage():
    publisher = Publisher("rtsp://localhost:8554/cozmo", hud=False)
    for stage, expected_step in (
        ("starting", "Starting connection"),
        ("connecting_wifi", "Connecting to robot Wi-Fi"),
        ("connecting_robot", "Connecting to robot"),
        ("cleaning_up", "Closing old connection"),
        ("retrying", "Waiting to retry"),
    ):
        title, detail = publisher._recovery_status(
            {"connected": False, "hardware_stage": stage}, had_video=True)
        assert title == "COZMO RECONNECTING"
        assert detail == expected_step
    title, detail = publisher._recovery_status(
        {"connected": False, "hardware_stage": "connecting_wifi"},
        had_video=False)
    assert title == "CONNECTING TO COZMO"
    assert detail == "Connecting to robot Wi-Fi"
    title, detail = publisher._recovery_status(
        {"connected": True, "hardware_stage": "online"}, had_video=True)
    assert title == "CAMERA RECONNECTING"
    assert detail == "Waiting for camera frames"


def test_recovery_slate_depends_on_supervisor_and_ignores_hud_toggle():
    publisher = Publisher("rtsp://localhost:8554/cozmo", hud=False)
    publisher.frame_count = 20
    baseline = Image.new("RGB", (320, 240), (12, 17, 23))
    connecting = publisher._offline_frame({
        "connected": False, "hardware_stage": "connecting_robot",
        "mrr_connected": True, "hardware_reconnect_attempts": 2,
        "last_hardware_error": "Reliable ACKs stalled"})
    retrying = publisher._offline_frame({
        "connected": False, "hardware_stage": "retrying",
        "mrr_connected": True, "hardware_reconnect_attempts": 3,
        "last_hardware_error": "Reliable ACKs stalled"})
    waiting = publisher._offline_frame({
        "connected": True, "hardware_stage": "online",
        "mrr_connected": True, "hardware_reconnect_attempts": 3})
    assert connecting.size == retrying.size == waiting.size == (320, 240)
    assert connecting.mode == "RGB"
    assert connecting.tobytes() != retrying.tobytes()
    assert retrying.tobytes() != waiting.tobytes()
    assert connecting.tobytes() != baseline.tobytes()
    assert connecting.getpixel((10, 10)) == (12, 17, 23)
    assert waiting.getpixel((10, 10)) == (12, 17, 23)
    # No redundant MRR status or error diagnostics on the RTSP slate.
    assert connecting.getpixel((25, 218)) == (12, 17, 23)
    assert connecting.getpixel((25, 190)) == (12, 17, 23)


def test_recovery_slate_handles_unknown_errors_and_counter_types():
    publisher = Publisher("rtsp://localhost:8554/cozmo")
    image = publisher._offline_frame({
        "hardware_stage": "retrying", "connected": False,
        "last_hardware_error": "very long error " * 100,
        "hardware_reconnect_attempts": None,
        "mrr_connected": False})
    assert image.size == (320, 240)
    assert image.mode == "RGB"
