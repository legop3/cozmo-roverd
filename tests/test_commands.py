import pytest
import threading
from cozmo_roverd.main import Bridge

class FakeRobot:
    headlight = False
    undock_active = False
    def __init__(self):
        self.calls = []
    def drive(self, left, right): self.calls.append(("drive", left, right))
    def interrupt_undock(self):
        self.calls.append(("interrupt_undock",))
        self.undock_active = False
    def head(self, angle): self.calls.append(("head", angle))
    def lift(self, height): self.calls.append(("lift", height))
    def light(self, state):
        self.headlight = state
        self.calls.append(("light", state))
    def note_tts(self, text): self.calls.append(("note_tts", text))
    def lift_motor(self, pwm): self.calls.append(("lift_motor", pwm))
    def start_undock(self, command_id, backoff, done):
        self.calls.append(("undock", command_id, backoff))
        done(command_id, None)
    def assist_docking(self): self.calls.append(("assist_docking",))
    def cancel_docking(self): self.calls.append(("cancel_docking",))
    def speak(self, text, speed=1.0): self.calls.append(("tts", text, speed))
    def set_camera_auto_exposure(self): self.calls.append(("camera_auto",))
    def set_camera_manual_exposure(self, ms): self.calls.append(("camera_manual", ms))

@pytest.fixture
def bridge():
    b = object.__new__(Bridge)
    b.cfg = type("Config", (), {"max_speed": 100})()
    b.robot = FakeRobot()
    b.head_degrees = 10
    b._reconnect_requested = threading.Event()
    b.report_result = lambda command_id, error=None: b.robot.calls.append(("result", command_id, error))
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


def test_brush_drives_lift_and_not_wheels(bridge):
    bridge.execute({"type": "motors", "motorPwm": {"main": 127, "side": 0, "vacuum": 0}})
    bridge.execute({"type": "motors", "motorPwm": {"main": -127, "side": 0, "vacuum": 0}})
    bridge.execute({"type": "motors", "motorPwm": {"main": 0, "side": 0, "vacuum": 0}})
    assert bridge.robot.calls[-3:] == [("lift_motor", 127), ("lift_motor", -127), ("lift_motor", 0)]


def test_undock_ack_and_completion(bridge):
    bridge.execute({"type": "undock", "id": "u1", "backoff": True})
    assert bridge.robot.calls[-2:] == [("undock", "u1", True), ("result", "u1", None)]


def test_dock_assist_and_cancel(bridge):
    bridge.execute({"type": "dockAssist"})
    bridge.execute({"type": "cancelDocking"})
    assert bridge.robot.calls[-2:] == [("assist_docking",), ("cancel_docking",)]


def test_tts_records_text_but_never_attempts_hardware_speaker(bridge):
    with pytest.raises(RuntimeError, match="speech disabled"):
        bridge.execute({"id": "s1", "type": "tts",
                        "tts": {"speak": True, "text": "Hi"}})
    assert bridge.robot.calls == [("note_tts", "Hi")]
    bridge.execute({"id": "s2", "type": "drive",
                    "driveDirect": {"left": 50, "right": -50}})
    assert bridge.robot.calls[-1] == ("drive", 50, -50)


def test_new_drive_interrupts_undock_instead_of_being_ignored(bridge):
    bridge.robot.undock_active = True
    bridge.execute({"type": "drive", "driveDirect": {"left": 100, "right": -100}})
    assert bridge.robot.calls == [("interrupt_undock",), ("drive", 100, -100)]


def test_tts_last_message_is_kept_even_without_audio(bridge):
    bridge.execute({"type": "tts", "tts": {"speak": False, "text": "Status update"}})
    assert bridge.robot.calls == [("note_tts", "Status update")]


def test_manual_face_and_volume_controls_are_removed(bridge):
    for control, value in (("face", "Hello"), ("volume", 60),
                           ("clear_face", True), ("light", True), ("light", False)):
        with pytest.raises(ValueError, match="unsupported peripheral"):
            bridge.execute({"type": "peripheral", "peripheral": {
                "id": "cozmo", "control": control, "value": value}})


def test_reconnect_button_only_requests_supervisor_reconnect(bridge):
    bridge.execute({"type": "peripheral", "peripheral": {
        "id": "cozmo", "control": "reconnect", "value": True}})
    assert bridge._reconnect_requested.is_set()
    # The command worker never touches the existing client directly.
    assert bridge.robot.calls == []
    bridge._reconnect_requested.clear()
    for control, value in (
        ("reconnect", False), ("reconnect", 1), ("reconnect", None),
        ("camera_auto_exposure", True), ("camera_manual_exposure", 20),
    ):
        with pytest.raises(ValueError, match="unsupported peripheral"):
            bridge.execute({"type": "peripheral", "peripheral": {
                "id": "cozmo", "control": control, "value": value}})
    assert not bridge._reconnect_requested.is_set()


def test_rover_state_is_sent_only_if_changed():
    import asyncio
    async def run():
        import asyncio
        import json
        from types import SimpleNamespace as NS

        class Socket:
            def __init__(self):
                self.messages = []
            async def send(self, value):
                self.messages.append(json.loads(value))

        b = object.__new__(Bridge)
        b.stop_event = asyncio.Event()
        b.state_dirty = asyncio.Event()
        b.hardware_ready = asyncio.Event()
        b.results = asyncio.Queue()
        state = {"type": "roverState", "state": {"docked": False, "battery": {"percent": 50}}}
        b.current_state = lambda: state
        ws = Socket()
        task = asyncio.create_task(b._send(ws, initial_state=state))
        await asyncio.sleep(0.12)
        assert ws.messages == []
        b.state_dirty.set()
        await asyncio.sleep(0.12)
        assert ws.messages == []
        state = {"type": "roverState", "state": {"docked": True, "battery": {"percent": 50}}}
        b.state_dirty.set()
        await asyncio.sleep(0.12)
        assert ws.messages == [state]
        b.stop_event.set()
        await task

    asyncio.run(run())