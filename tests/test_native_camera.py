from unittest.mock import Mock
from PIL import Image
import pytest
from cozmo_roverd.config import camera_options
from cozmo_roverd.robot import Robot
from pycozmo import protocol_encoder as p

def make_robot():
    return Robot(max_speed=250, on_state=Mock(), on_sensor=Mock(), on_frame=Mock())

def test_camera_options():
    assert camera_options({}) == {"camera_color": True, "camera_resolution": "QVGA"}
    with pytest.raises(ValueError):
        camera_options({"camera_resolution": "bogus"})
    with pytest.raises(ValueError):
        camera_options({"camera_color": "false"})

def test_experimental_resolution_migrates_to_qvga():
    assert camera_options({"camera_resolution": "VGA"})["camera_resolution"] == "QVGA"

def test_camera_mode_toggle_only_sends_color_command():
    robot = make_robot()
    robot.client = Mock()
    robot.ready = True
    robot.toggle_camera_color()
    assert robot.camera_input["camera_color"] is False
    robot.toggle_camera_color()
    assert robot.camera_input["camera_color"] is True
    packets = [call.args[0] for call in robot.client.conn.send.call_args_list]
    assert all(isinstance(packet, p.EnableColorImages) for packet in packets)
    assert [bool(packet.enable) for packet in packets] == [False, True]
    robot.client.enable_camera.assert_not_called()

def test_native_metadata():
    robot = make_robot()
    image = Image.new("RGB", (320, 240))
    image.info.update(cozmo_native_frame=Image.new("RGB", (160, 240)),
                      cozmo_resolution="QVGA", cozmo_encoding="JPEGMinimizedGray", cozmo_color=True)
    robot._camera_image(image)
    actual = robot.camera_input_diagnostics()["received"]
    assert actual["native_size"] == [160, 240]
    assert actual["decoded_size"] == [320, 240]
    assert actual["color"] is True
