"""A temporary Cozmo outage must not terminate the MRR rover session."""
import asyncio
import json
from types import SimpleNamespace as NS
from cozmo_roverd.main import Bridge
from cozmo_roverd.robot_packet import BatteryTracker


class FakeSocket:
    def __init__(self, messages=()):
        self.sent = []
        self.messages = list(messages)

    async def send(self, data):
        self.sent.append(json.loads(data))

    def __aiter__(self):
        async def generator():
            for message in self.messages:
                yield json.dumps(message)
        return generator()


def disconnected_bridge():
    bridge = object.__new__(Bridge)
    bridge.hardware_ready = asyncio.Event()
    bridge.stop_event = asyncio.Event()
    bridge.state_dirty = asyncio.Event()
    bridge.results = asyncio.Queue()
    bridge.robot = NS(
        battery=BatteryTracker(), headlight=False,
        ready=False, dock_assist=False, docking_phase=None,
        latest={},
    )
    return bridge


def test_state_sender_stays_active_with_hardware_offline():
    async def run():
        bridge = disconnected_bridge()
        socket = FakeSocket()
        task = asyncio.create_task(bridge._send(socket))
        await asyncio.sleep(0.12)
        bridge.stop_event.set()
        await task
        return socket.sent
    sent = asyncio.run(run())
    assert sent
    states = [message for message in sent if message["type"] == "roverState"]
    assert states
    assert states[0]["state"]["drivingMode"] is False


def test_offline_commands_receive_explicit_errors_without_closing_socket():
    async def run():
        bridge = disconnected_bridge()
        socket = FakeSocket([
            {"id": "drive-1", "type": "drive", "driveDirect": {"left": 100, "right": 100}},
            {"id": "drive-2", "type": "drive", "driveDirect": {"left": 0, "right": 0}},
        ])
        await bridge._receive(socket)
        return socket.sent
    results = asyncio.run(run())
    assert [item["id"] for item in results] == ["drive-1", "drive-2"]
    assert all(item["type"] == "ack" and item["status"] == "error" for item in results)
    assert all("reconnecting" in item["error"] for item in results)



def test_failed_robot_connection_retries_and_recovers_without_wifi_reassociation(monkeypatch):
    import time
    from cozmo_roverd.robot_packet import BatteryTracker
    async def run():
        bridge = disconnected_bridge()
        bridge.hardware_stage = "starting"
        bridge.last_hardware_error = None
        bridge.hardware_reconnect_attempts = 0
        bridge.publish_state = lambda: None
        calls = []
        loop = asyncio.get_running_loop()
        class Wifi:
            def wait(self):
                calls.append("wifi")
            def active(self):
                return True
        class Robot:
            ready = False
            battery = BatteryTracker()
            last_sensor = time.monotonic()
            def connect(self):
                calls.append("connect")
                if calls.count("connect") == 1:
                    raise RuntimeError("first handshake failed")
                self.ready = True
                loop.call_soon_threadsafe(bridge.stop_event.set)
            def close(self):
                calls.append("close")
                self.ready = False
        bridge.wifi = Wifi()
        bridge.robot = Robot()
        await asyncio.wait_for(bridge._hardware(), timeout=6)
        return calls, bridge.hardware_reconnect_attempts
    calls, attempts = asyncio.run(run())
    assert calls.count("connect") == 2
    assert calls.count("wifi") == 2
    assert calls.count("close") == 2
    assert attempts == 2
