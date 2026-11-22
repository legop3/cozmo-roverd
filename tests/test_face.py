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
    assert robot.refresh_face()
    assert robot.last_tts == "This is a message"
    assert len(robot.client.images) == 2
    assert all(isinstance(im, Image.Image) for im in robot.client.images)
    assert all(im.size == (128, 32) for im in robot.client.images)



def test_tts_generates_wav_and_queues_verified_audio_frames(monkeypatch):
    from pathlib import Path
    from subprocess import CompletedProcess
    import pycozmo
    import cozmo_roverd.robot as robot_module

    robot = robot_with_face()
    enabled, playback = [], []

    class Controller:
        def enable_animations(self, enabled_now):
            enabled.append(enabled_now)
        def play_audio(self, frames):
            playback.append(frames)

    robot.client.anim_controller = Controller()
    def fake_subprocess(args, **kwargs):
        if "-w" in args:
            Path(args[args.index("-w") + 1]).write_bytes(b"RIFF" + bytes(100))
        else:
            Path(args[-1]).write_bytes(b"RIFF" + bytes(100))
        return CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(robot_module.subprocess, "run", fake_subprocess)
    monkeypatch.setattr(pycozmo.audio, "load_wav", lambda path: ["audio-1", "audio-2"])

    robot.note_tts("Hello Cozmo")
    robot.speak("Hello Cozmo")
    assert enabled == [True]
    assert playback == [["audio-1", "audio-2"]]
    assert robot.last_tts_frames == 2
    assert robot.last_tts_error is None
    assert robot.last_tts == "Hello Cozmo"


def test_tts_failure_is_visible_in_face_diagnostics(monkeypatch):
    import cozmo_roverd.robot as robot_module
    robot = robot_with_face()
    def broken(*args, **kwargs):
        raise FileNotFoundError("espeak-ng missing")
    monkeypatch.setattr(robot_module.subprocess, "run", broken)
    import pytest
    with pytest.raises(RuntimeError, match="TTS failed"):
        robot.speak("test")
    assert "espeak-ng" in robot.last_tts_error


def test_generated_widgets_change_with_battery_and_telemetry():
    baseline = status_image(
        voltage=3.8, percent=10, charging=False, wheels=(0, 0),
        cliff=False, picked_up=False)
    charged = status_image(
        voltage=4.2, percent=95, charging=True, docked=True,
        wheels=(250, -250), cliff=True, picked_up=True,
        audio_error="speech playback failed")
    assert baseline.size == charged.size == (128, 32)
    assert baseline.mode == charged.mode == "1"
    assert baseline.tobytes() != charged.tobytes()


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


def test_lean_animation_sender_does_not_flood_idle_robot(monkeypatch):
    import time
    from types import SimpleNamespace as NS
    from collections import deque
    import pycozmo
    from cozmo_roverd.robot import _limit_animation_idle_traffic

    sent = []
    events = []
    audio = deque()
    images = deque()
    class Queue:
        def get(self):
            return (audio.popleft() if audio else None,
                    images.popleft() if images else None, None)
    controller = NS(queue=Queue(), animations_enabled=True,
                    playing_audio=False, procedural_face_enabled=False,
                    playing_animation=False, image_displayed=True,
                    last_image_pkt="last-image")
    client = NS(anim_controller=controller,
                conn=NS(send=sent.append, post_event=lambda *a: events.append(a)))
    _limit_animation_idle_traffic(client)
    # 100 animation ticks should not send 100 images or 100 silence packets.
    controller._cozmo_image_refresh_at = time.monotonic()
    for _ in range(100):
        controller._run_frame()
    assert sent == []
    images.append("new-image")
    controller._run_frame()
    assert sent == ["new-image"]
    audio.extend(["audio-1", "audio-2"])
    controller._run_frame()
    controller._run_frame()
    controller._run_frame()
    controller._run_frame()
    assert sent[1:3] == ["audio-1", "audio-2"]
    assert sum(isinstance(pkt, pycozmo.protocol_encoder.OutputSilence) for pkt in sent) == 1
    assert len(events) == 1



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


def test_face_does_not_interfere_with_active_speech_audio():
    from types import SimpleNamespace as NS
    robot = robot_with_face()
    robot.client.anim_controller = NS(playing_audio=True)
    assert not robot.refresh_face()
    robot.client.anim_controller.playing_audio = False
    assert robot.refresh_face()



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


def test_tts_sends_only_one_unacknowledged_audio_frame_at_a_time():
    from collections import deque
    from threading import Lock
    from queue import Queue
    from types import SimpleNamespace as NS
    from cozmo_roverd.robot import _limit_animation_idle_traffic
    from pycozmo.protocol_encoder import OutputSilence

    sent = []
    window = NS(next_seq=0, expected_seq=0, max_seq=65534)
    sender = NS(lock=Lock(), window=window, queue=Queue())
    class AudioQueue:
        def __init__(self):
            self.lock = Lock()
            self.audio_queue = deque(["audio1", "audio2", "audio3"])
        def get(self):
            with self.lock:
                return (self.audio_queue.popleft() if self.audio_queue else None,
                        None, None)
    controller = NS(queue=AudioQueue(), animations_enabled=True,
                    playing_audio=False, procedural_face_enabled=False,
                    playing_animation=False, image_displayed=False)
    def send(packet):
        sent.append(packet)
        # Mimic sender thread collecting a queued packet into the reliable
        # window, but *not* acknowledging it until the test advances ACK.
        window.next_seq += 1
    cli = NS(anim_controller=controller,
             conn=NS(send=send, send_thread=sender, post_event=lambda *a: None))
    _limit_animation_idle_traffic(cli)
    controller._run_frame()
    assert sent == ["audio1"]
    for _ in range(20):
        controller._run_frame()
    assert sent == ["audio1"]
    assert list(controller.queue.audio_queue) == ["audio2", "audio3"]
    window.expected_seq = 1
    controller._run_frame()
    assert sent == ["audio1", "audio2"]
    controller._run_frame()
    assert len(sent) == 2
    window.expected_seq = 2
    controller._run_frame()
    assert sent[-1] == "audio3"
    controller._run_frame()
    assert len(sent) == 3  # trailing silence must await final ACK
    window.expected_seq = 3
    controller._run_frame()
    assert isinstance(sent[-1], OutputSilence)
    assert controller._cozmo_audio_frames_submitted == 3


def test_idle_face_clock_is_low_rate_and_only_runs_when_connected():
    from types import SimpleNamespace as NS
    from cozmo_roverd.robot import _limit_animation_idle_traffic
    from pycozmo.protocol_encoder import OutputSilence
    import time

    sent = []
    controller = NS(
        queue=NS(get=lambda: (None, None, None)),
        animations_enabled=True, playing_audio=False,
        procedural_face_enabled=False, playing_animation=False,
        image_displayed=True)
    cli = NS(anim_controller=controller,
             conn=NS(send=sent.append, CONNECTED=3, state=3,
                     post_event=lambda *a: None))
    _limit_animation_idle_traffic(cli)
    controller._cozmo_last_idle_silence_at = time.monotonic()
    for _ in range(30):
        controller._run_frame()
    assert not sent
    controller._cozmo_last_idle_silence_at -= 2
    controller._run_frame()
    assert len(sent) == 1
    assert isinstance(sent[0], OutputSilence)
    controller._run_frame()
    assert len(sent) == 1



def test_tts_limits_firmware_playback_buffer_even_when_network_ack_is_fast():
    from collections import deque
    from threading import Lock
    from queue import Queue
    from types import SimpleNamespace as NS
    from cozmo_roverd.robot import _limit_animation_idle_traffic

    sent = []
    window = NS(next_seq=0, expected_seq=0, max_seq=65534)
    sender = NS(lock=Lock(), window=window, queue=Queue())
    class AudioQueue:
        def __init__(self):
            self.lock = Lock()
            self.audio_queue = deque(["a", "b", "c", "d"])
        def get(self):
            with self.lock:
                return (self.audio_queue.popleft() if self.audio_queue else None,
                        None, None)
    controller = NS(queue=AudioQueue(), animations_enabled=True,
                    playing_audio=False, procedural_face_enabled=False,
                    playing_animation=False, image_displayed=False,
                    num_audio_frames_played=100)
    def send(packet):
        sent.append(packet)
        window.next_seq += 1
    cli = NS(anim_controller=controller,
             conn=NS(send=send, send_thread=sender, post_event=lambda *args: None))
    _limit_animation_idle_traffic(cli)
    controller._cozmo_audio_played_baseline = 100
    controller._run_frame()  # a
    window.expected_seq = 1
    controller._run_frame()  # b
    window.expected_seq = 2
    controller._run_frame()  # blocked by two audio frames not yet played
    assert sent == ["a", "b"]
    assert list(controller.queue.audio_queue) == ["c", "d"]
    controller.num_audio_frames_played = 101
    controller._run_frame()  # c after firmware reports progress
    assert sent == ["a", "b", "c"]



def test_tts_defers_screen_keyframe_instead_of_transmitting_both_together():
    from collections import deque
    from threading import Lock
    from types import SimpleNamespace as NS
    from cozmo_roverd.robot import _limit_animation_idle_traffic

    sent = []
    class Q:
        def __init__(self):
            self.lock = Lock()
            self.audio_queue = deque(["audio-frame"])
            self.image_queue = deque(["face-keyframe"])
        def get(self):
            with self.lock:
                audio = self.audio_queue.popleft() if self.audio_queue else None
                image = self.image_queue.popleft() if self.image_queue else None
                return audio, image, None
    q = Q()
    controller = NS(queue=q, animations_enabled=True, playing_audio=False,
                    procedural_face_enabled=False, playing_animation=False,
                    image_displayed=True, last_image_pkt=None)
    cli = NS(anim_controller=controller,
             conn=NS(send=sent.append, post_event=lambda *a: None))
    _limit_animation_idle_traffic(cli)
    controller._run_frame()
    assert sent == ["audio-frame"]
    assert list(q.image_queue) == ["face-keyframe"]
    controller._run_frame()
    assert sent[-1] == "face-keyframe"


def test_tts_played_counter_is_read_from_animation_controller():
    from types import SimpleNamespace as NS
    robot = robot_with_face()
    from threading import Lock
    robot.client.anim_controller = NS(
        num_audio_frames_played=57,
        queue=NS(lock=Lock(), audio_queue=[]),
    )
    assert robot.speaker_diagnostics()["robot_reported_audio_frames"] == 57



def test_audio_bootstraps_first_frame_then_requires_ack_and_keyframes():
    from collections import deque
    from threading import Lock
    from queue import Queue
    from types import SimpleNamespace as NS
    from cozmo_roverd.robot import _limit_animation_idle_traffic
    from pycozmo import protocol_encoder

    callbacks = {}
    sent = []
    class AudioQueue:
        def __init__(self):
            self.lock = Lock()
            self.audio_queue = deque(["first", "second"])
        def get(self):
            with self.lock:
                return self.audio_queue.popleft() if self.audio_queue else None, None, None
    window = NS(next_seq=0, expected_seq=0, max_seq=65534)
    sender = NS(lock=Lock(), window=window, queue=Queue())
    controller = NS(queue=AudioQueue(), animations_enabled=True,
                    playing_audio=False, image_displayed=False,
                    procedural_face_enabled=False, playing_animation=False)
    def send(packet):
        sent.append(packet)
        window.next_seq += 1
    cli = NS(anim_controller=controller,
             conn=NS(send=send, send_thread=sender, post_event=lambda *a: None),
             add_handler=lambda packet, cb: callbacks.setdefault(packet, cb))
    _limit_animation_idle_traffic(cli)
    controller._run_frame()
    assert sent == ["first"]  # no preexisting Keyframe is required
    controller._run_frame()
    assert sent == ["first"]  # first packet still awaiting network ACK
    window.expected_seq = 1
    controller._run_frame()
    assert sent == ["first"]  # ACK alone isn't animation readiness
    callbacks[protocol_encoder.Keyframe](cli, protocol_encoder.Keyframe())
    controller._run_frame()
    assert sent == ["first", "second"]


def test_speech_aborts_when_first_audio_is_acked_but_no_keyframe_arrives():
    from collections import deque
    from threading import Lock
    from queue import Queue
    from types import SimpleNamespace as NS
    from unittest.mock import patch
    from cozmo_roverd.robot import _limit_animation_idle_traffic

    sent = []
    class AudioQueue:
        def __init__(self):
            self.lock = Lock()
            self.audio_queue = deque(["first", "second"])
        def get(self):
            with self.lock:
                return self.audio_queue.popleft() if self.audio_queue else None, None, None
    controller = NS(queue=AudioQueue(), animations_enabled=True,
                    playing_audio=False, image_displayed=False,
                    procedural_face_enabled=False, playing_animation=False)
    window = NS(next_seq=0, expected_seq=0, max_seq=65534)
    sender = NS(lock=Lock(), window=window, queue=Queue())
    def send(pkt):
        sent.append(pkt)
        window.next_seq += 1
    cli = NS(anim_controller=controller,
             conn=NS(send=send, send_thread=sender, post_event=lambda *a: None),
             add_handler=lambda *a: None)
    _limit_animation_idle_traffic(cli)
    controller._run_frame()
    assert sent == ["first"]
    window.expected_seq = 1
    with patch("cozmo_roverd.robot.time.monotonic", side_effect=[12.1, 14.5]):
        controller._run_frame()
        controller._run_frame()
    assert controller._cozmo_audio_aborted
    assert list(controller.queue.audio_queue) == []
    assert sent == ["first"]


def test_operator_face_gives_latest_tts_a_full_width_readable_row():
    image = status_image(voltage=3.98, percent=81,
                         charging=True, last_tts="HELLO WORLD")
    empty = status_image(voltage=3.98, percent=81,
                         charging=True, last_tts="")
    # The message occupies the prominent bottom row, not a 6px
    # clipped footer. Battery voltage and gauge occupy the top band.
    assert image.crop((2, 21, 127, 32)).getbbox() is not None
    assert image.crop((2, 21, 127, 32)).tobytes() != (
        empty.crop((2, 21, 127, 32)).tobytes())
    assert image.crop((3, 0, 128, 12)).getbbox() is not None
    # Long messages should scroll without changing top-row battery data.
    long0 = status_image(voltage=3.98, percent=81, last_tts="A VERY LONG MESSAGE ACROSS COZMO DISPLAY", scroll=0)
    long1 = status_image(voltage=3.98, percent=81, last_tts="A VERY LONG MESSAGE ACROSS COZMO DISPLAY", scroll=5)
    assert long0.crop((0, 21, 128, 32)).tobytes() != long1.crop((0, 21, 128, 32)).tobytes()
    assert long0.crop((0, 0, 128, 21)).tobytes() == long1.crop((0, 0, 128, 21)).tobytes()
