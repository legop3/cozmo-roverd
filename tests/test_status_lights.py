"""Cozmo backpack and physical charger LEDs use the real object protocol."""
from types import SimpleNamespace as NS
import pycozmo
from cozmo_roverd.robot import Robot
from cozmo_roverd.status_lights import charger_lights, charger_signature


class FakeConn:
    def __init__(self):
        self.sent = []
    def send(self, pkt):
        self.sent.append(pkt)


class FakeClient:
    def __init__(self):
        self.conn = FakeConn()
        self.available_objects = {}
        self.connected_objects = {}


def make_robot():
    robot = Robot(max_speed=250, on_state=lambda: None,
                  on_sensor=lambda: None, on_frame=lambda f: None)
    robot.client = FakeClient()
    robot.ready = True
    return robot


def test_missing_charger_is_optional_and_silent():
    robot = make_robot()
    assert not robot.refresh_charger_lights()
    assert not robot.client.conn.sent


def test_discovers_only_charger_not_cubes():
    robot = make_robot()
    typ = pycozmo.protocol_encoder.ObjectType
    robot.client.available_objects[17] = NS(object_type=typ.Block_LIGHTCUBE1)
    assert not robot.refresh_charger_lights()
    robot.client.available_objects[99] = NS(object_type=typ.Charger_Basic)
    assert robot.refresh_charger_lights()
    sent = robot.client.conn.sent
    assert len(sent) == 1
    assert isinstance(sent[0], pycozmo.protocol_encoder.ObjectConnect)
    assert sent[0].factory_id == 99
    assert sent[0].connect is True
    # Do not select an object until the real connection ID is reported.
    assert not robot.refresh_charger_lights()
    assert len(robot.client.conn.sent) == 1


def test_connected_charger_selected_by_object_type_and_factory():
    robot = make_robot()
    p = pycozmo.protocol_encoder
    robot.client.connected_objects[2] = {
        "factory_id": 17, "object_type": p.ObjectType.Block_LIGHTCUBE1}
    robot.client.connected_objects[8] = {
        "factory_id": 99, "object_type": p.ObjectType.Charger_Basic}
    robot.battery.docked = True
    robot.battery.charging = True
    assert robot.refresh_charger_lights()
    select, lights = robot.client.conn.sent
    assert isinstance(select, p.CubeId)
    assert select.object_id == 8
    assert isinstance(lights, p.CubeLights)
    assert len(lights.states) == 4
    assert lights.states[0].on_color != lights.states[0].off_color
    assert not robot.refresh_charger_lights()
    assert len(robot.client.conn.sent) == 2


def test_charger_docking_transition_updates_and_off_state():
    robot = make_robot()
    p = pycozmo.protocol_encoder
    robot.client.connected_objects[6] = {
        "factory_id": 42, "object_type": p.ObjectType.Charger_Basic}
    robot.battery.docked = True
    robot.battery.charging = False
    assert robot.refresh_charger_lights()
    green = robot.client.conn.sent[-1].states[0]
    assert green.on_color == green.off_color
    robot.battery.docked = False
    assert robot.refresh_charger_lights()
    off = robot.client.conn.sent[-1].states
    assert all(led.on_color == 0 and led.off_color == 0 for led in off)
    assert len(robot.client.conn.sent) == 4


def test_charger_led_semantics_and_signature():
    assert charger_signature(docked=False, charging=False, docking=False) == (
        False, None, False)
    assert charger_signature(docked=True, charging=None, docking=False) != (
        charger_signature(docked=True, charging=False, docking=False))
    assert len(charger_lights(docked=True, charging=True)) == 4
    assert len(charger_lights(docked=False, charging=False)) == 4


def test_side_status_leds_use_red_channel_only():
    """Cozmo's outer two backpack LEDs are physically red-only."""
    from cozmo_roverd.status_lights import light_states
    for robot_online in (True, False):
        for mrr_online in (True, False):
            left, front, middle, rear, right = light_states(
                robot_online=robot_online, mrr_online=mrr_online,
                docked=False, charging=False)
            for side in (left, right):
                assert side.on_color & 0x7c00, "red channel must be illuminated"
                assert side.on_color & 0x03ff == 0, "side LEDs cannot emit green or blue"
            if robot_online:
                assert left.on_color == left.off_color
            else:
                assert left.on_color != left.off_color
            if mrr_online:
                assert right.on_color == right.off_color
            else:
                assert right.on_color != right.off_color


def test_optional_led_features_default_off_and_enable_independently(monkeypatch):
    from cozmo_roverd.main import _env_enabled
    monkeypatch.delenv("COZMO_BACKPACK_STATUS", raising=False)
    monkeypatch.delenv("COZMO_CHARGER_STATUS", raising=False)
    assert not _env_enabled("COZMO_BACKPACK_STATUS")
    assert not _env_enabled("COZMO_CHARGER_STATUS")
    monkeypatch.setenv("COZMO_BACKPACK_STATUS", "1")
    assert _env_enabled("COZMO_BACKPACK_STATUS")
    assert not _env_enabled("COZMO_CHARGER_STATUS")
    monkeypatch.setenv("COZMO_BACKPACK_STATUS", "0")
    monkeypatch.setenv("COZMO_CHARGER_STATUS", "yes")
    assert not _env_enabled("COZMO_BACKPACK_STATUS", default=True)
    assert _env_enabled("COZMO_CHARGER_STATUS")


def test_minimal_backpack_status_sends_one_packet_on_state_change_only():
    p = pycozmo.protocol_encoder
    robot = make_robot()
    # Start with MRR offline, then connect, then disconnect.
    assert robot.refresh_status_lights(mrr_connected=False)
    assert len(robot.client.conn.sent) == 1
    packet = robot.client.conn.sent[0]
    assert isinstance(packet, p.LightStateSide)
    assert packet.states[0].on_color != 0
    assert packet.states[0].on_color == packet.states[0].off_color
    assert packet.states[1].on_color == 0
    assert not robot.refresh_status_lights(mrr_connected=False)
    assert len(robot.client.conn.sent) == 1
    assert robot.refresh_status_lights(mrr_connected=True)
    assert len(robot.client.conn.sent) == 2
    assert isinstance(robot.client.conn.sent[1], p.LightStateSide)
    assert robot.client.conn.sent[1].states[1].on_color != 0
    assert not robot.refresh_status_lights(mrr_connected=True)
    assert robot.refresh_status_lights(mrr_connected=False)
    assert len(robot.client.conn.sent) == 3
    # Side update must never enqueue a LightStateCenter packet.
    assert all(isinstance(pkt, p.LightStateSide)
               for pkt in robot.client.conn.sent)


def test_backpack_status_does_not_modify_center_lights_or_need_docking_state():
    p = pycozmo.protocol_encoder
    robot = make_robot()
    robot.battery.docked = True
    robot.battery.charging = True
    assert robot.refresh_status_lights(mrr_connected=True)
    robot.battery.docked = False
    robot.battery.charging = False
    assert not robot.refresh_status_lights(mrr_connected=True)
    assert len(robot.client.conn.sent) == 1
    assert isinstance(robot.client.conn.sent[0], p.LightStateSide)
