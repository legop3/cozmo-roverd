"""A temporary Cozmo outage must not terminate the MRR rover session."""
import asyncio
import json
import threading
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
    bridge._reconnect_requested = threading.Event()
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


def test_live_camera_suppresses_robotstate_silence_reconnect():
    from cozmo_roverd.main import Bridge
    from types import SimpleNamespace as NS
    bridge = object.__new__(Bridge)
    bridge.publisher = NS(last_frame_time=100.0)
    assert bridge._camera_link_active(102.0)
    assert not bridge._camera_link_active(106.0)
    bridge.publisher = NS(last_frame_time=None)
    assert not bridge._camera_link_active(102.0)
    bridge.publisher = None
    assert not bridge._camera_link_active(102.0)


def test_mrr_grace_period_and_recovery_reset():
    from cozmo_roverd.main import Bridge
    bridge = disconnected_bridge()
    bridge._hardware_offline_since = 100.0
    assert bridge._mrr_offline_remaining(now=100.0) == 600
    assert bridge._mrr_offline_remaining(now=699.1) == 1
    assert bridge._mrr_offline_remaining(now=700.0) == 0
    assert bridge._mrr_offline_remaining(now=710.0) == 0
    # A real reconnection cancels the outage timer immediately.
    bridge.hardware_ready.set()
    bridge._hardware_offline_since = None
    assert bridge._mrr_offline_remaining(now=710.0) is None
    # A subsequent failure starts a fresh 10-minute window.
    bridge.hardware_ready.clear()
    bridge._hardware_offline_since = 720.0
    assert bridge._mrr_offline_remaining(now=720.0) == 600


def test_mrr_availability_watcher_expires_only_after_grace():
    async def run():
        bridge = disconnected_bridge()
        import time
        bridge._hardware_offline_since = time.monotonic()
        # Initially under grace, cancellation must not expire registration.
        watching = asyncio.create_task(bridge._watch_mrr_hardware())
        await asyncio.sleep(0.01)
        assert not watching.done()
        watching.cancel()
        await asyncio.gather(watching, return_exceptions=True)
        # When grace is exhausted, registration should end immediately.
        import time
        bridge._hardware_offline_since = time.monotonic() - 601.0
        # Force a sufficiently long elapsed period without changing clocks.
        assert bridge._mrr_offline_remaining() == 0
        await asyncio.wait_for(bridge._watch_mrr_hardware(), timeout=0.2)
    asyncio.run(run())


def test_manual_reconnect_uses_existing_hardware_supervisor():
    async def run():
        bridge = disconnected_bridge()
        bridge.hardware_stage = "starting"
        bridge.last_hardware_error = None
        bridge.hardware_reconnect_attempts = 0
        bridge.publish_state = lambda: None
        bridge._diag_last_sample = 0
        bridge.publisher = None
        bridge.mrr_connected = True
        calls = []
        connected_twice = asyncio.Event()
        class Wifi:
            def wait(self):
                calls.append("wifi")
            def active(self):
                return True
        class Robot:
            ready = False
            battery = BatteryTracker()
            last_sensor = 0
            packets_received = 0
            def connect(self):
                calls.append("connect")
                self.ready = True
                if calls.count("connect") == 2:
                    bridge.loop.call_soon_threadsafe(connected_twice.set)
            def close(self):
                calls.append("close")
                self.ready = False
            def transport_diagnostics(self):
                return None
        bridge.loop = asyncio.get_running_loop()
        bridge.wifi = Wifi()
        bridge.robot = Robot()
        supervisor = asyncio.create_task(bridge._hardware())
        try:
            await asyncio.wait_for(bridge.hardware_ready.wait(), timeout=3)
            bridge.execute({"type": "peripheral", "peripheral": {
                "id": "cozmo", "control": "reconnect", "value": True}})
            await asyncio.wait_for(connected_twice.wait(), timeout=7)
            assert bridge.hardware_ready.is_set()
            assert calls[:4] == ["wifi", "connect", "close", "wifi"]
            assert calls.count("connect") == 2
            assert bridge.mrr_connected is True
            assert not bridge._reconnect_requested.is_set()
        finally:
            bridge.stop_event.set()
            await asyncio.wait_for(supervisor, timeout=4)
    asyncio.run(run())
