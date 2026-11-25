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

def test_native_resolution_command_and_grayscale():
    robot = make_robot()
    robot.camera_input = camera_options({"camera_resolution": "VGA", "camera_color": False})
    cli = Mock()
    robot._send_camera_mode(cli, True)
    packets = [call.args[0] for call in cli.conn.send.call_args_list]
    assert isinstance(packets[0], p.EnableCamera)
    assert packets[0].image_resolution == p.ImageResolution.VGA
    assert not packets[1].enable

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
