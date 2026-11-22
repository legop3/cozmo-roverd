"""Camera frame path regression tests (no Cozmo or RTSP server needed)."""
from PIL import Image
from cozmo_roverd.media import Publisher


def test_color_frame_converts_to_ffmpeg_rgb24():
    publisher = Publisher("rtsp://localhost:8554/cozmo")
    publisher.running.set()
    publisher.submit(Image.new("RGB", (320, 240), (10, 20, 30)))
    data = publisher.frames.get_nowait()
    assert len(data) == 320 * 240 * 3
    assert data[:3] == bytes((10, 20, 30))


def test_gray_frame_also_supported():
    publisher = Publisher("rtsp://localhost:8554/cozmo")
    publisher.running.set()
    publisher.submit(Image.new("L", (320, 240), 128))
    data = publisher.frames.get_nowait()
    assert len(data) == 320 * 240 * 3
    assert data[:3] == bytes((128, 128, 128))


def test_latest_frame_wins_when_encoder_behind():
    publisher = Publisher("rtsp://localhost:8554/cozmo")
    publisher.running.set()
    for color in ((1, 2, 3), (4, 5, 6), (7, 8, 9)):
        publisher.submit(Image.new("RGB", (320, 240), color))
    assert publisher.frames.get_nowait()[:3] == bytes((4, 5, 6))
    assert publisher.frames.get_nowait()[:3] == bytes((7, 8, 9))
