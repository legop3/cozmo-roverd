"""Persistent disconnect diagnostics are bounded, private and nonintrusive."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from cozmo_roverd.diagnostics import Recorder, _safe_command


def test_command_redacts_speech_and_ignores_unknown_payload():
    cmd = _safe_command({"type": "tts",
                         "tts": {"text": "SECRET WORDS", "speak": True}})
    assert cmd == {"type": "tts", "speak": True, "text_redacted": True}
    assert "SECRET" not in repr(cmd)
    assert _safe_command({"type": "drive", "driveDirect": {"left": 8, "right": -3}}) == (
        {"type": "drive", "left": 8, "right": -3})
    assert _safe_command({"type": "unknown", "password": "private"}) == (
        {"type": "unknown"})


def test_disconnect_snapshot_preserves_transport_and_last_protocol_packets(tmp_path):
    recorder = Recorder(tmp_path)
    try:
        recorder.command({"type": "drive", "driveDirect": {"left": 90, "right": 80}})
        recorder.command({"type": "tts", "tts": {"text": "do not store me", "speak": False}})
        recorder.sample(stage="online", transport={
            "pending_reliable_packets": 12, "next_ack_expected": 44},
            packet_count=76, last_sensor_age=2.0)
        # PyCozmo uses this exact format for command queue events.
        class DisplayImage:
            id = 18
            def __len__(self):
                return 330
            def __repr__(self):
                raise AssertionError("Never serialize raw packet payloads")
        logging.getLogger("pycozmo.protocol").debug("Sent %s", DisplayImage())
        recorder.incident(reason="ACK stalled", stage="online", last_sensor_age=12.0)
    finally:
        recorder.close()
    lines = [json.loads(x) for x in (tmp_path / "incidents.jsonl").read_text().splitlines()]
    snap = next(x for x in lines if x["event"] == "disconnection_snapshot")
    assert snap["reason"] == "ACK stalled"
    assert snap["last_events"][-2]["type"] == "drive"
    assert snap["last_events"][-1]["text_redacted"]
    assert snap["transport_history"][-1]["transport"]["next_ack_expected"] == 44
    assert snap["recent_protocol_packets"][-1]["packet"] == "DisplayImage"
    assert snap["recent_protocol_packets"][-1]["bytes"] == 330
    assert "do not store me" not in (tmp_path / "incidents.jsonl").read_text()
    assert "wire tx DisplayImage bytes=330" in (tmp_path / "protocol.log").read_text()


def test_protocol_logger_is_restored_after_shutdown(tmp_path):
    logger = logging.getLogger("pycozmo.protocol")
    prior_level, prior_propagate = logger.level, logger.propagate
    recorder = Recorder(tmp_path)
    try:
        assert logger.level == logging.DEBUG
        assert not logger.propagate
    finally:
        recorder.close()
    assert logger.level == prior_level
    assert logger.propagate == prior_propagate


def test_incident_history_is_append_only_across_restarts(tmp_path):
    first = Recorder(tmp_path)
    try:
        first.incident(reason="first_failure", stage="connecting_robot")
    finally:
        first.close()
    second = Recorder(tmp_path)
    try:
        second.incident(reason="second_failure", stage="online")
    finally:
        second.close()
    failures = [
        json.loads(line)["reason"]
        for line in (tmp_path / "incidents.jsonl").read_text().splitlines()
        if json.loads(line).get("event") == "disconnection_snapshot"
    ]
    assert failures == ["first_failure", "second_failure"]
    assert not (tmp_path / "incidents.jsonl.1").exists()
