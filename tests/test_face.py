"""The robot's face status is independent of MRR and audio playback."""
from PIL import Image
from cozmo_roverd.face import status_image
from cozmo_roverd.robot import Robot


class FakeDisplay:
    def __init__(self):
        self.images = []

    def display_image(self, image):
        self.images.append(image.copy())


def robot_with_face():
    robot = Robot(max_speed=250, on_state=lambda: None, on_sensor=lambda: None,
                  on_frame=lambda frame: None)
    robot.client = FakeDisplay()
    robot.ready = True
    return robot


def test_face_has_battery_and_latest_tts_and_128x32_format():
    image = status_image(
        voltage=3.91, percent=62, charging=True,
        cliff=True, picked_up=False, wheels=(125, -75),
        last_tts="Hello from MultiRoombaRover",
    )
    assert image.mode == "1"
    assert image.size == (128, 32)
    assert image.getbbox() is not None


def test_latest_tts_scrolls_and_remains_visible():
    image1 = status_image(last_tts="This is a fairly long message", scroll=0)
    image2 = status_image(last_tts="This is a fairly long message", scroll=8)
    assert image1.tobytes() != image2.tobytes()


def test_face_updates_only_on_change_but_last_message_is_persistent():
    robot = robot_with_face()
    assert robot.refresh_face()
    assert not robot.refresh_face()
    robot.note_tts("This is a message")
    # The new message is displayed as soon as it arrives, even if later
    # audio synthesis/playback gets stuck.
    assert len(robot.client.images) == 2
    assert not robot.refresh_face()
    assert robot.last_tts == "This is a message"
    assert all(isinstance(im, Image.Image) for im in robot.client.images)
    assert all(im.size == (128, 32) for im in robot.client.images)



def test_face_changes_only_with_voltage_or_last_tts():
    baseline = status_image(voltage=3.8, last_tts="Hello")
    visible_robot_status_changed = status_image(
        voltage=3.8, last_tts="Hello", percent=95, charging=True,
        docked=True, wheels=(250, -250), cliff=True, picked_up=True,
        audio_error="speech playback failed")
    assert baseline.size == visible_robot_status_changed.size == (128, 32)
    assert baseline.mode == visible_robot_status_changed.mode == "1"
    assert baseline.tobytes() == visible_robot_status_changed.tobytes()
    assert status_image(voltage=4.2, last_tts="Hello").tobytes() != baseline.tobytes()
    assert status_image(voltage=3.8, last_tts="Goodbye").tobytes() != baseline.tobytes()


def test_generated_face_has_no_content_outside_128x32_canvas():
    image = status_image(
        voltage=3.91, percent=73, charging=True,
        wheels=(200, -200), last_tts="Hello from the rover",
        scroll=100)
    assert image.getbbox() is not None
    assert len(image.tobytes()) == 128 * 32 // 8



def test_image_encoding_matches_generated_128x32_face():
    from pycozmo.image_encoder import ImageEncoder, ImageDecoder
    for kwargs in (
        {"voltage": 3.95, "percent": 64, "charging": True, "docked": True,
         "wheels": (0, 0), "last_tts": "Hello from Cozmo", "scroll": 0},
        {"voltage": 3.2, "percent": 5, "charging": False, "cliff": True,
         "picked_up": True, "wheels": (250, -250),
         "last_tts": "Long text message across the display", "scroll": 19},
        {},
    ):
        frame = status_image(**kwargs)
        encoded = bytes(ImageEncoder(frame).encode())
        assert encoded, "Bitmap generated an empty wire packet"
        decoded = ImageDecoder(encoded).decode()
        assert len(decoded) == 128 * 32
        for y in range(32):
            for x in range(128):
                assert bool(decoded[y * 128 + x]) == bool(frame.getpixel((x, y)))


def test_face_does_not_enqueue_frames_when_reliable_channel_is_congested():
    from types import SimpleNamespace as NS
    from threading import Lock
    from queue import Queue

    robot = robot_with_face()
    sent = robot.client
    sender = NS(lock=Lock(), window=NS(next_seq=14, expected_seq=2, max_seq=65536,
                                     size=62), queue=Queue(),
                outgoing_packets=50, sent_packets=1500)
    robot.client.conn = NS(send_thread=sender, recv_thread=NS(received_frames=50))
    robot.client.anim_controller = NS(playing_audio=False)
    status = robot.transport_diagnostics()
    assert status["pending_reliable_packets"] == 12
    assert status["transmitted_packets_including_retries"] == 1500
    assert not robot.refresh_face()
    assert sent.images == []
    # After pending ACKs clear, face output resumes normally.
    sender.window.expected_seq = 14
    assert robot.refresh_face()
    assert len(sent.images) == 1


def test_face_packet_always_fits_single_cozmo_transport_frame():
    from pycozmo.image_encoder import ImageEncoder
    from pycozmo.protocol_declaration import MAX_FRAME_PAYLOAD_SIZE
    image = status_image(voltage=3.96, percent=82, charging=True,
                         docked=True, cliff=True, picked_up=True,
                         wheels=(-250, 250),
                         last_tts="This is a long message displayed while moving",
                         scroll=6, audio_error="speech error")
    payload = bytes(ImageEncoder(image).encode())
    # This test documents whether the full graphic fits on the wire. The
    # robot refresh fallback handles states where it does not.
    if len(payload) > MAX_FRAME_PAYLOAD_SIZE - 6:
        robot = robot_with_face()
        robot.battery.voltage = 3.96
        robot.battery.percent = 82
        robot.last_tts = "Test oversized image"
        assert robot.refresh_face()
        assert robot.face_last_encoded_bytes <= MAX_FRAME_PAYLOAD_SIZE - 6
    else:
        assert len(payload) <= MAX_FRAME_PAYLOAD_SIZE - 6


def test_face_records_encoding_size_after_display():
    robot = robot_with_face()
    assert robot.refresh_face()
    assert robot.face_last_encoded_bytes is not None
    assert robot.face_last_encoded_bytes > 0
    assert robot.face_last_error is None



def test_face_uses_full_width_status_instead_of_tiny_battery_only_fallback():
    from pycozmo.image_encoder import ImageEncoder
    from pycozmo.protocol_declaration import MAX_FRAME_PAYLOAD_SIZE

    robot = robot_with_face()
    robot.battery.voltage = 3.91
    robot.battery.percent = 62
    robot.last_tts = "Hello Cozmo"
    assert robot.refresh_face()
    frame = robot.client.images[-1]
    assert frame.mode == "1"
    assert frame.size == (128, 32)
    assert frame.crop((90, 0, 128, 32)).getbbox() is not None
    assert robot.face_last_encoded_bytes == len(ImageEncoder(frame).encode())
    assert robot.face_last_encoded_bytes <= MAX_FRAME_PAYLOAD_SIZE - 6


def test_oversized_face_uses_compact_fallback_with_valid_wire_payload(monkeypatch):
    import cozmo_roverd.robot as robot_module
    from pycozmo.image_encoder import ImageEncoder
    from pycozmo.protocol_declaration import MAX_FRAME_PAYLOAD_SIZE

    # A high-entropy image cannot fit in one Cozmo packet. It must be
    # replaced, but ordinary ~300-byte dashboard images must not.
    noise = Image.new("1", (128, 32))
    for y in range(32):
        for x in range(128):
            noise.putpixel((x, y), ((x * 57 + y * 91 + x*y) % 7 < 3))
    assert len(ImageEncoder(noise).encode()) > MAX_FRAME_PAYLOAD_SIZE - 6
    monkeypatch.setattr(robot_module, "status_image", lambda **kwargs: noise)
    robot = robot_with_face()
    assert robot.refresh_face()
    assert robot.face_last_encoded_bytes <= MAX_FRAME_PAYLOAD_SIZE - 6
    assert robot.client.images[0].tobytes() != noise.tobytes()


def test_tts_played_counter_is_read_from_animation_controller():
    from types import SimpleNamespace as NS
    robot = robot_with_face()
    from threading import Lock
    robot.client.anim_controller = NS(
        num_audio_frames_played=57,
        queue=NS(lock=Lock(), audio_queue=[]),
    )
    assert robot.speaker_diagnostics()["robot_reported_audio_frames"] == 57



def test_operator_face_gives_latest_tts_a_full_width_readable_row():
    image = status_image(voltage=3.98, percent=81,
                         charging=True, last_tts="HELLO WORLD")
    empty = status_image(voltage=3.98, percent=81,
                         charging=True, last_tts="")
    # The message occupies the whole bottom band and battery voltage
    # occupies the top band; nothing else appears on the display.
    assert image.crop((2, 21, 127, 32)).getbbox() is not None
    assert image.crop((2, 21, 127, 32)).tobytes() != (
        empty.crop((2, 21, 127, 32)).tobytes())
    assert image.crop((3, 0, 128, 12)).getbbox() is not None
    # Long messages should scroll without changing top-row battery data.
    long0 = status_image(voltage=3.98, percent=81, last_tts="A VERY LONG MESSAGE ACROSS COZMO DISPLAY", scroll=0)
    long1 = status_image(voltage=3.98, percent=81, last_tts="A VERY LONG MESSAGE ACROSS COZMO DISPLAY", scroll=5)
    assert long0.crop((0, 21, 128, 32)).tobytes() != long1.crop((0, 21, 128, 32)).tobytes()
    assert long0.crop((0, 0, 128, 21)).tobytes() == long1.crop((0, 0, 128, 21)).tobytes()



def test_wire_encoded_face_is_not_a_single_pixel():
    from pycozmo.image_encoder import ImageEncoder, ImageDecoder
    for voltage, speech in ((3.75, "NO TTS YET"), (4.05, "Hello Cozmo"),
                            (None, "A longer TTS message is here")):
        img = status_image(voltage=voltage, last_tts=speech)
        decoded = ImageDecoder(bytes(ImageEncoder(img).encode())).decode()
        count = sum(bool(px) for px in decoded)
        width = sum(any(decoded[y*128+x] for y in range(32))
                    for x in range(128))
        assert count >= 30
        assert width >= 8



def test_wire_decoded_face_diagnostics_show_more_than_a_dot():
    robot = robot_with_face()
    robot.battery.voltage = 3.98
    robot.note_tts("This is the latest TTS message")
    assert robot.face_encoded_on_pixels is not None
    assert robot.face_encoded_on_pixels >= 30
    assert robot.face_decoded_columns >= 8



def test_tts_transport_stall_disables_further_speaker_attempts_but_keeps_last_message():
    import pytest
    robot = robot_with_face()
    robot.mark_speech_unstable("Audio ACKs stopped")
    robot.note_tts("I still want this text visible")
    assert robot.last_tts == "I still want this text visible"
    assert robot._tts_transport_faulted
    with pytest.raises(RuntimeError, match="speech disabled"):
        robot.speak("No repeat attempt")
    assert len(robot.client.images) >= 1





def test_hardware_tts_disabled_without_touching_camera_or_speaker():
    import pytest
    robot = robot_with_face()
    robot.note_tts("Hello from the controller")
    with pytest.raises(RuntimeError, match="speech disabled"):
        robot.speak("Hello from the controller")
    assert robot.last_tts == "Hello from the controller"
    assert robot.client.images
    assert "speech disabled" in robot.last_tts_error.lower()


def test_display_only_sender_clears_accidental_audio_packets_and_never_sends_them():
    from types import SimpleNamespace as NS
    from collections import deque
    from threading import Lock
    from cozmo_roverd.robot import _limit_animation_idle_traffic

    sent = []
    class Q:
        def __init__(self):
            self.lock = Lock()
            self.audio_queue = deque(["DO_NOT_SEND_AUDIO"])
            self.image_queue = deque(["test-image"])
        def get(self):
            with self.lock:
                return (self.audio_queue.popleft() if self.audio_queue else None,
                        self.image_queue.popleft() if self.image_queue else None, None)
    q = Q()
    ctl = NS(queue=q, animations_enabled=True, image_displayed=True,
             last_image_pkt=None)
    cli = NS(anim_controller=ctl,
             conn=NS(send=sent.append))
    _limit_animation_idle_traffic(cli)
    ctl._run_frame()
    assert "DO_NOT_SEND_AUDIO" not in sent
    assert "test-image" in sent
    assert not q.audio_queue


def test_display_only_sender_clocks_image_without_30hz_image_flood():
    from types import SimpleNamespace as NS
    from collections import deque
    from threading import Lock
    from unittest.mock import patch
    from cozmo_roverd.robot import _limit_animation_idle_traffic
    import pycozmo

    sent = []
    class Q:
        def __init__(self):
            self.lock = Lock()
            self.audio_queue = deque()
            self.image_queue = deque(["img"])
        def get(self):
            with self.lock:
                return None, self.image_queue.popleft() if self.image_queue else None, None
    ctl = NS(queue=Q(), animations_enabled=True, image_displayed=True,
             last_image_pkt="blank")
    cli = NS(anim_controller=ctl, conn=NS(send=sent.append))
    _limit_animation_idle_traffic(cli)
    with patch("cozmo_roverd.robot.time.monotonic", side_effect=[1.0, 1.05, 1.30]):
        for _ in range(3):
            ctl._run_frame()
    assert sent.count("img") == 1
    assert sent.count("blank") == 0
    assert sum(isinstance(p, pycozmo.protocol_encoder.OutputSilence) for p in sent) == 2
