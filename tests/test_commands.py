import pytest
from cozmo_roverd.main import Bridge

class FakeRobot:
    headlight = False
    def __init__(self):
        self.calls = []
    def drive(self, left, right): self.calls.append(("drive", left, right))
    def head(self, angle): self.calls.append(("head", angle))
    def lift(self, height): self.calls.append(("lift", height))
    def light(self, state):
        self.headlight = state
        self.calls.append(("light", state))
    def face(self, text): self.calls.append(("face", text))

@pytest.fixture
def bridge():
    b = object.__new__(Bridge)
    b.cfg = type("Config", (), {"max_speed": 100})()
    b.robot = FakeRobot()
    b.head_degrees = 10
    return b

def test_drive_limit(bridge):
    bridge.execute({"type": "drive", "driveDirect": {"left": 200, "right": -120}})
    assert bridge.robot.calls[-1] == ("drive", 100, -100)

def test_drive_invalid_input(bridge):
    with pytest.raises(ValueError):
        bridge.execute({"type": "drive", "driveDirect": {"left": True, "right": 0}})

def test_head_nudge_clamp(bridge):
    bridge.execute({"type": "servo", "servo": {"nudge": 100}})
    assert bridge.robot.calls[-1] == ("head", 44)

def test_lift(bridge):
    bridge.execute({"type": "peripheral", "peripheral": {"id": "cozmo", "control": "lift", "value": 52}})
    assert bridge.robot.calls[-1] == ("lift", 52)

def test_unsupported_commands_rejected(bridge):
    with pytest.raises(ValueError):
        bridge.execute({"type": "autoDock"})
    with pytest.raises(ValueError):
        bridge.execute({"type": "raw", "raw": "AA=="})

def test_headlight_toggle(bridge):
    bridge.execute({"type": "headlight", "headlight": {"action": "toggle"}})
    bridge.execute({"type": "headlight", "headlight": {"action": "toggle"}})
    assert bridge.robot.calls == [("light", True), ("light", False)]
