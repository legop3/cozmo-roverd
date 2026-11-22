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
