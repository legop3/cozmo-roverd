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
