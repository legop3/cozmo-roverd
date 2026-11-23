"""The robot's face status is independent of MRR and audio playback."""
from PIL import Image
from cozmo_roverd.face import status_image
from cozmo_roverd.robot import Robot
from pycozmo.protocol_declaration import MAX_FRAME_PAYLOAD_SIZE


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
    # Tests exercise the renderer independently of real-hardware transport.
    robot._face_transport_enabled = True
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


def test_long_tts_is_clipped_without_paging_and_keeps_battery_icon():
    from cozmo_roverd.face import static_status_image
    from pycozmo.image_encoder import ImageEncoder

    message = "THIS IS A LONG MESSAGE " * 30
    first = static_status_image(voltage=3.91, percent=60, last_tts=message)
    extra = static_status_image(voltage=3.91, percent=60, last_tts=message + "EXTRA")
    assert first.size == (128, 32)
    assert first.tobytes() == extra.tobytes()
    assert first.crop((94, 19, 128, 32)).getbbox() is not None
    assert len(ImageEncoder(first).encode()) <= MAX_FRAME_PAYLOAD_SIZE - 6


def test_default_static_face_sends_full_battery_panel_and_readable_text():
    from cozmo_roverd.face import static_status_image
    from pycozmo.image_encoder import ImageEncoder

    for message in ("NO MESSAGE", "Hello", "Hello from Cozmo",
                    "Here is a message longer than the display can show " * 15):
        robot = robot_with_face()
        robot.battery.voltage = 3.91
        robot.battery.percent = 65
        robot.last_tts = message
        assert robot.refresh_face(), f"Display skipped message: {message[:60]}"
        image = robot.client.images[-1]
        assert image.crop((94, 19, 128, 32)).getbbox() is not None
        assert image.crop((0, 0, 90, 32)).getbbox() is not None
        assert 0 < len(ImageEncoder(image).encode()) <= MAX_FRAME_PAYLOAD_SIZE - 6
        assert robot.face_last_error is None


def test_short_messages_never_fall_back_to_battery_only():
    from cozmo_roverd.face import static_status_image
    from pycozmo.image_encoder import ImageEncoder, ImageDecoder

    for text in ("HI", "Hello", "NO MESSAGE", "Ready", "Battery low"):
        image = static_status_image(
            voltage=3.91, percent=60, charging=False, last_tts=text)
        # Left-hand text must contain pixels regardless of text length.
        assert image.crop((0, 0, 90, 32)).getbbox() is not None, text
        # The original battery icon remains present as well.
        assert image.crop((94, 19, 128, 32)).getbbox() is not None, text
        encoded = bytes(ImageEncoder(image).encode())
        assert len(encoded) <= MAX_FRAME_PAYLOAD_SIZE - 6
        decoded = ImageDecoder(encoded).decode()
        assert any(decoded[y * 128 + x]
                   for y in range(32) for x in range(90)), text


def test_static_face_clips_very_long_tts_without_sending_one_character_pages():
    from cozmo_roverd.face import static_status_image

    prefix = "A long message that cannot possibly fit on this display. "
    first = static_status_image(
        voltage=3.9, percent=40, last_tts=prefix + "A" * 1000)
    second = static_status_image(
        voltage=3.9, percent=40, last_tts=prefix + "B" * 1000)
    assert first.tobytes() == second.tobytes()
    assert first.crop((0, 0, 90, 32)).getbbox() is not None
    assert first.crop((94, 19, 128, 32)).getbbox() is not None


def test_face_updates_obey_cooldown_and_last_message_is_persistent():
    robot = robot_with_face()
    assert robot.refresh_face()
    assert not robot.refresh_face()
    robot.note_tts("This is a message")
    assert robot.last_tts == "This is a message"
    # New TTS must not bypass the cooldown and jam the animation channel.
    assert len(robot.client.images) == 1
    robot._face_sent_at = 0.0
    assert robot.refresh_face()
    assert len(robot.client.images) == 2
    assert not robot.refresh_face()


def test_face_shows_battery_state_in_distinct_panel():
    baseline = status_image(voltage=3.8, percent=30, last_tts="Hello")
    alternate = status_image(
        voltage=3.8, percent=90, charging=True, last_tts="Hello")
    assert baseline.size == alternate.size == (128, 32)
    assert baseline.mode == alternate.mode == "1"
    assert baseline.tobytes() != alternate.tobytes()
    assert status_image(voltage=4.2, percent=30, last_tts="Hello").tobytes() != baseline.tobytes()
    assert status_image(voltage=3.8, percent=30, last_tts="Goodbye").tobytes() != baseline.tobytes()
    # Battery status must not encroach on the TTS panel.
    assert baseline.crop((0, 0, 90, 32)).tobytes() == alternate.crop((0, 0, 90, 32)).tobytes()


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
    # The normal large-font image should use the UDP payload budget.
    robot = robot_with_face()
    robot.battery.voltage = 3.96
    robot.battery.percent = 82
    robot.last_tts = "Test oversized image"
    assert robot.refresh_face()
    assert robot.face_last_encoded_bytes <= MAX_FRAME_PAYLOAD_SIZE - 6


def test_long_tts_sends_one_static_bounded_frame():
    from pycozmo.image_encoder import ImageEncoder

    robot = robot_with_face()
    robot.battery.voltage = 3.91
    robot.battery.percent = 62
    robot.last_tts = "The rover is explaining a complicated message. " * 10
    assert robot.refresh_face()
    frame = robot.client.images[-1]
    assert len(ImageEncoder(frame).encode()) <= MAX_FRAME_PAYLOAD_SIZE - 6
    assert frame.crop((94, 19, 128, 32)).getbbox() is not None
    assert not robot.refresh_face()


def test_face_records_encoding_size_after_display():
    robot = robot_with_face()
    assert robot.refresh_face()
    assert robot.face_last_encoded_bytes is not None
    assert robot.face_last_encoded_bytes > 0
    assert robot.face_last_error is None



def test_status_keeps_battery_icon_and_voltage_for_short_message():
    from cozmo_roverd.face import static_status_image
    from pycozmo.image_encoder import ImageEncoder
    frame = static_status_image(voltage=3.91, percent=62, last_tts="Hello")
    assert frame.mode == "1"
    assert frame.size == (128, 32)
    assert frame.crop((90, 0, 128, 32)).getbbox() is not None
    assert frame.crop((0, 0, 90, 32)).getbbox() is not None
    assert len(ImageEncoder(frame).encode()) <= MAX_FRAME_PAYLOAD_SIZE - 6


def test_sender_never_enqueues_oversized_images(monkeypatch):
    import cozmo_roverd.face as face_module
    from pycozmo.image_encoder import ImageEncoder

    noise = Image.new("1", (128, 32))
    for y in range(32):
        for x in range(128):
            noise.putpixel((x, y), ((x * 57 + y * 91 + x*y) % 7 < 3))
    assert len(ImageEncoder(noise).encode()) > MAX_FRAME_PAYLOAD_SIZE - 6
    monkeypatch.setattr(face_module, "static_status_image", lambda **kwargs: noise)
    robot = robot_with_face()
    assert not robot.refresh_face()
    assert robot.client.images == []


def test_tts_played_counter_is_read_from_animation_controller():
    from types import SimpleNamespace as NS
    robot = robot_with_face()
    from threading import Lock
    robot.client.anim_controller = NS(
        num_audio_frames_played=57,
        queue=NS(lock=Lock(), audio_queue=[]),
    )
    assert robot.speaker_diagnostics()["robot_reported_audio_frames"] == 57



def test_operator_face_has_independent_battery_panel_and_adaptive_pages():
    from cozmo_roverd.face import _layout
    from PIL import ImageDraw
    short = status_image(voltage=3.98, percent=80, last_tts="HELLO")
    long_message = "A VERY LONG MESSAGE THAT SHOULD WRAP OVER SEVERAL READABLE PAGES"
    first = status_image(voltage=3.98, percent=80, last_tts=long_message, scroll=0)
    second = status_image(voltage=3.98, percent=80, last_tts=long_message, scroll=1)
    assert first.tobytes() != second.tobytes()
    # The entire battery panel stays unchanged as the message pages rotate.
    assert first.crop((91, 0, 128, 32)).tobytes() == second.crop((91, 0, 128, 32)).tobytes()
    assert short.crop((0, 0, 88, 32)).getbbox() is not None
    canvas = Image.new("1", (128, 32))
    font, pages, step = _layout(long_message, ImageDraw.Draw(canvas))
    assert len(pages) >= 2
    assert step >= 12
    assert font is not None


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


def test_static_face_uses_upstream_packet_order_without_30hz_loop():
    from types import SimpleNamespace as NS
    from pycozmo.protocol_encoder import OutputSilence, DisplayImage

    robot = robot_with_face()
    raw_packets = []
    robot.client.conn = NS(send=raw_packets.append)
    assert robot.refresh_face()
    assert len(raw_packets) == 3
    assert isinstance(raw_packets[0], OutputSilence)
    assert isinstance(raw_packets[1], DisplayImage)
    assert isinstance(raw_packets[2], OutputSilence)
    assert len(raw_packets[1].image) <= MAX_FRAME_PAYLOAD_SIZE - 6
    assert not robot.advance_face_display()
    assert not robot.refresh_face()
    assert len(raw_packets) == 3


def test_static_face_respects_congested_transport():
    from types import SimpleNamespace as NS
    from threading import Lock
    from queue import Queue

    robot = robot_with_face()
    sender = NS(lock=Lock(), window=NS(next_seq=12, expected_seq=0,
                                      max_seq=65536), queue=Queue())
    raw_packets = []
    robot.client.conn = NS(send=raw_packets.append, send_thread=sender)
    assert not robot.refresh_face()
    assert raw_packets == []
    sender.window.expected_seq = 12
    assert robot.refresh_face()
    assert len(raw_packets) == 3
    assert not robot.advance_face_display()


def test_face_resumes_automatically_after_traffic_backoff(monkeypatch):
    from types import SimpleNamespace as NS
    from threading import Lock
    from queue import Queue
    import cozmo_roverd.robot as module

    robot = robot_with_face()
    sent = []
    sender = NS(lock=Lock(), window=NS(next_seq=9, expected_seq=0,
                                      max_seq=65536), queue=Queue())
    robot.client.conn = NS(send=sent.append, send_thread=sender)
    clock = [100.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    assert not robot.refresh_face()
    clock[0] = 104.0
    assert not robot.refresh_face()
    assert robot._face_suspended_until == 114.0
    sender.window.expected_seq = 9
    clock[0] = 110.0
    assert not robot.refresh_face()
    clock[0] = 115.0
    assert robot.refresh_face()
    assert len(sent) == 3
    assert robot._face_blocked_since is None


def test_pycozmo_runtime_method_replacements_are_gone():
    import cozmo_roverd.robot as robot_module
    assert not hasattr(robot_module, "_limit_animation_idle_traffic")
    assert not hasattr(robot_module, "_fix_pycozmo_packet_batching")


def test_headless_fedora_without_truetype_fonts_still_draws_face(monkeypatch):
    import cozmo_roverd.face as face_module
    monkeypatch.setattr(face_module, "_FONT_CHOICES", ())
    monkeypatch.setattr(face_module, "_VOLTAGE_CHOICES", ())
    image = face_module.status_image(voltage=3.98, percent=50, last_tts="Hello Cozmo")
    assert image.mode == "1"
    assert image.size == (128, 32)
    assert image.crop((0, 0, 90, 32)).getbbox() is not None
    assert image.crop((92, 0, 128, 32)).getbbox() is not None
    occupied_columns = sum(
        image.crop((x, 0, x + 1, 32)).getbbox() is not None
        for x in range(128))
    assert occupied_columns >= 25


def test_large_status_preserves_native_scanline_bitmap():
    image = status_image(voltage=3.91, percent=60, last_tts="HELLO FROM COZMO")
    assert image.size == (128, 32)
    assert image.crop((0, 0, 90, 32)).getbbox() is not None
    assert image.crop((92, 0, 128, 32)).getbbox() is not None
    # No pre-interlacing: both even and odd rows contain image data.
    assert any(image.crop((0, y, 128, y + 1)).getbbox() is not None
               for y in range(0, 32, 2))
    assert any(image.crop((0, y, 128, y + 1)).getbbox() is not None
               for y in range(1, 32, 2))


def test_wrap_splits_long_unbroken_words_without_clipping():
    from cozmo_roverd.face import _wrap, _font, _FONT_CHOICES
    from PIL import ImageDraw
    canvas = Image.new("1", (128, 32))
    draw = ImageDraw.Draw(canvas)
    message = "SUPERCALIFRAGILISTICEXPIALIDOCIOUS" * 2
    lines = _wrap(message, draw, _font(15, _FONT_CHOICES), 85)
    assert len(lines) >= 2
    assert "".join(lines) == message


def test_short_message_uses_larger_font_than_paginated_long_message():
    from cozmo_roverd.face import _layout, _height
    from PIL import ImageDraw
    draw = ImageDraw.Draw(Image.new("1", (128, 32)))
    big_font, _, _ = _layout("HI", draw)
    long_font, pages, _ = _layout("A speech message that cannot fit on one small screen" * 3, draw)
    assert _height(draw, big_font) > _height(draw, long_font)
    assert len(pages) > 1


def test_battery_gauge_fill_and_voltage_change_independently_of_speech():
    speech = "Ready"
    empty = status_image(voltage=3.2, percent=0, last_tts=speech)
    full = status_image(voltage=3.2, percent=100, last_tts=speech)
    charging = status_image(voltage=3.2, percent=100, charging=True, last_tts=speech)
    assert empty.crop((0, 0, 90, 32)).tobytes() == full.crop((0, 0, 90, 32)).tobytes()
    assert empty.crop((94, 19, 128, 32)).tobytes() != full.crop((94, 19, 128, 32)).tobytes()
    assert full.tobytes() != charging.tobytes()


def test_live_face_transport_defaults_to_bounded_static_sender():
    robot = Robot(max_speed=250, on_state=lambda: None,
                  on_sensor=lambda: None, on_frame=lambda frame: None)
    robot.client = FakeDisplay()
    robot.ready = True
    assert robot._face_transport_enabled is True
    assert robot.refresh_face() is True
    assert robot.advance_face_display() is False
    assert len(robot.client.images) == 1
