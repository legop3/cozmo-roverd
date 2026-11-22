"""Static backpack colors and charger dock/assist light transitions."""
from types import SimpleNamespace as NS
import pycozmo
from cozmo_roverd.robot import Robot
from cozmo_roverd.status_lights import (
    backpack_lights, charger_lights, charger_signature,
)


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


def test_backpack_has_two_red_only_sides_and_three_different_rgb_colors():
    sides = backpack_lights()
    assert len(sides) == 5
    assert sides[0].on_color == sides[4].on_color
    assert all(s.on_color == s.off_color for s in sides)
    assert len({s.on_color for s in sides[1:4]}) == 3
    assert sides[0].on_color & 0x7c00
    assert sides[0].on_color & 0x03ff == 0


def test_backpack_sent_only_once_per_robot_connection_regardless_of_mrr():
    p = pycozmo.protocol_encoder
    robot = make_robot()
    assert robot.refresh_status_lights(mrr_connected=False)
    assert len(robot.client.conn.sent) == 2
    center, side = robot.client.conn.sent
    assert isinstance(center, p.LightStateCenter)
    assert isinstance(side, p.LightStateSide)
    assert len(center.states) == 3
    assert len(side.states) == 2
    assert not robot.refresh_status_lights(mrr_connected=True)
    assert not robot.refresh_status_lights(mrr_connected=False)
    assert len(robot.client.conn.sent) == 2
    robot._status_light_signature = None  # reconnect resets this
    assert robot.refresh_status_lights(mrr_connected=True)
    assert len(robot.client.conn.sent) == 4


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
    assert not robot.refresh_charger_lights()
    assert len(robot.client.conn.sent) == 1


def test_connected_charger_is_selected_by_object_type_and_all_four_are_green():
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
    assert len({s.on_color for s in lights.states}) == 1
    assert all(s.on_color == s.off_color for s in lights.states)
    assert not robot.refresh_charger_lights()


def test_charger_red_undocked_purple_assisting_green_docked():
    robot = make_robot()
    p = pycozmo.protocol_encoder
    robot.client.connected_objects[6] = {
        "factory_id": 42, "object_type": p.ObjectType.Charger_Basic}
    robot.battery.docked = False
    assert robot.refresh_charger_lights()
    red = robot.client.conn.sent[-1].states[0].on_color
    assert red != 0
    assert not robot.refresh_charger_lights()
    robot.dock_assist = True
    assert robot.refresh_charger_lights()
    purple = robot.client.conn.sent[-1].states[0].on_color
    assert purple not in (0, red)
    robot.dock_assist = False
    robot.battery.docked = True
    assert robot.refresh_charger_lights()
    green = robot.client.conn.sent[-1].states[0].on_color
    assert green not in (0, red, purple)
    robot.battery.charging = True
    assert not robot.refresh_charger_lights()
    robot.battery.charging = False
    assert not robot.refresh_charger_lights()
    robot.battery.docked = False
    assert robot.refresh_charger_lights()
    assert robot.client.conn.sent[-1].states[0].on_color == red


def test_charger_unknown_dock_is_red_and_assist_overrides_it():
    assert charger_signature(docked=False) == "undocked"
    assert charger_signature(docked=None) == "undocked"
    assert charger_signature(docked=True, charging=True) == "docked"
    assert charger_signature(docked=True, charging=False) == "docked"
    assert charger_signature(docked=False, docking=True) == "assist"
    assert charger_lights(docked=None)[0].on_color == charger_lights(
        docked=False)[0].on_color
    assert charger_lights(docked=False, docking=True)[0].on_color != (
        charger_lights(docked=False)[0].on_color)
