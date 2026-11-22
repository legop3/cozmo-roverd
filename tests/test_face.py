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
            Path(args[args.index("-y") + 1]).write_bytes(b"RIFF" + bytes(100))
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
