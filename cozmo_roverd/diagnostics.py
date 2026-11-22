"""Append-only Cozmo incidents and a bounded passive PyCozmo protocol trace.

No changes to PyCozmo methods, robot packets, or connection timing. The
protocol filter strips payloads before formatting the upstream 'Sent/Got'
messages, then ships records to a bounded queue and rotating file.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
import os
from pathlib import Path
from queue import Queue
import threading
import time

DEFAULT_DIR = "/var/log/cozmo-roverd"
_active = None


def _utc():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_command(msg):
    """Never persist TTS text, credentials, identifiers or raw message JSON."""
    kind = str(msg.get("type", "unknown"))[:40]
    safe = {"type": kind}
    if kind == "drive":
        p = msg.get("driveDirect")
        if isinstance(p, dict):
            safe["left"] = p.get("left")
            safe["right"] = p.get("right")
    elif kind == "servo":
        p = msg.get("servo")
        if isinstance(p, dict):
            safe["servo"] = {k: v for k, v in p.items() if k in ("angle", "nudge")}
    elif kind == "motors":
        p = msg.get("motorPwm")
        if isinstance(p, dict):
            safe["main"] = p.get("main")
    elif kind == "headlight":
        p = msg.get("headlight")
        if isinstance(p, dict):
            safe["action"] = p.get("action")
    elif kind == "peripheral":
        p = msg.get("peripheral")
        if isinstance(p, dict):
            safe["control"] = p.get("control")
            safe["value"] = p.get("value")
    elif kind == "tts":
        p = msg.get("tts")
        safe["speak"] = p.get("speak", True) if isinstance(p, dict) else None
        safe["text_redacted"] = True
    return safe


class _ProtocolMetadata(logging.Filter):
    """Strip huge packet reprs and retain a short, in-memory wire history."""
    def __init__(self, recorder):
        super().__init__()
        self.recorder = recorder

    def filter(self, record):
        if record.name == "pycozmo.protocol" and record.args:
            fmt = record.msg if isinstance(record.msg, str) else ""
            if fmt.startswith(("Sent %s", "Got  %s")):
                pkt = record.args[0]
                try:
                    length = len(pkt)
                except (TypeError, ValueError, AttributeError):
                    length = None
                record.msg = "wire %s %s bytes=%s id=%s"
                record.args = ("tx" if fmt.startswith("Sent") else "rx",
                               type(pkt).__name__, length, getattr(pkt, "id", None))
                item = {"utc": _utc(), "monotonic": round(time.monotonic(), 3),
                        "direction": record.args[0],
                        "packet": record.args[1],
                        "bytes": length, "id": record.args[3]}
                with self.recorder.lock:
                    self.recorder.packets.append(item)
        return True


class _DropWhenFull(QueueHandler):
    """Never block a PyCozmo receive/send callback on disk I/O."""
    def __init__(self, queue):
        super().__init__(queue)
        self.dropped = 0

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except Exception:
            self.dropped += 1


class Recorder:
    def __init__(self, directory=DEFAULT_DIR):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.incidents_path = self.directory / "incidents.jsonl"
        self.events = deque(maxlen=120)
        self.transport = deque(maxlen=90)
        self.packets = deque(maxlen=300)
        self.lock = threading.Lock()
        self._queue = Queue(maxsize=8192)
        self._trace_handler = _DropWhenFull(self._queue)
        self._trace_handler.addFilter(_ProtocolMetadata(self))
        self._trace_output = RotatingFileHandler(
            self.directory / "protocol.log", maxBytes=8 * 1024 * 1024,
            backupCount=4, encoding="utf-8")
        self._trace_output.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S"))
        self._listener = QueueListener(self._queue, self._trace_output,
                                       respect_handler_level=True)
        # Disconnect evidence is append-only: never discard older incidents.
        # Only the high-volume packet trace rotates. Existing .1..9 archives
        # from the previous incident rotation remain untouched.
        self._incident_output = logging.FileHandler(
            self.incidents_path, mode="a", encoding="utf-8")
        self._incident_output.setFormatter(logging.Formatter("%(message)s"))
        self._incident_logger = logging.getLogger("cozmo-roverd.incident-file")
        self._incident_logger.addHandler(self._incident_output)
        self._incident_logger.propagate = False
        self._incident_logger.setLevel(logging.INFO)
        self._protocol_logger = logging.getLogger("pycozmo.protocol")
        self._original_level = self._protocol_logger.level
        self._original_propagate = self._protocol_logger.propagate
        # Packet trace is written only to its rotating file, not journald.
        self._protocol_logger.propagate = False
        self._protocol_logger.setLevel(logging.DEBUG)
        self._protocol_logger.addHandler(self._trace_handler)
        self._listener.start()
        self.event("recorder_started", pid=os.getpid(), directory=str(self.directory))

    def _write(self, row):
        try:
            self._incident_logger.info(json.dumps(row, separators=(",", ":"),
                                                  default=str))
            self._incident_output.flush()
        except Exception:
            logging.getLogger(__name__).exception("Unable to persist Cozmo diagnostic event")

    def event(self, name, **fields):
        row = {"utc": _utc(), "monotonic": round(time.monotonic(), 3),
               "event": name, **fields}
        with self.lock:
            self.events.append(row)
        self._write(row)

    def command(self, msg):
        # High-rate drive commands live in the bounded incident ring.
        # They are persisted together when a failure occurs, rather than
        # synchronously writing to disk for every motor command.
        row = {"utc": _utc(), "monotonic": round(time.monotonic(), 3),
               "event": "mrr_command", **_safe_command(msg)}
        with self.lock:
            self.events.append(row)

    def sample(self, *, stage, transport=None, packet_count=None,
               last_sensor_age=None, camera_age=None, battery_voltage=None):
        row = {"utc": _utc(), "monotonic": round(time.monotonic(), 3),
               "stage": stage, "transport": transport,
               "robotstate_packets": packet_count,
               "sensor_age_seconds": last_sensor_age,
               "camera_age_seconds": camera_age,
               "battery_voltage": battery_voltage}
        with self.lock:
            self.transport.append(row)

    def incident(self, *, reason, stage, **context):
        with self.lock:
            recent = list(self.events)
            samples = list(self.transport)
            packets = list(self.packets)
        # Captured BEFORE cleanup destroys the live transport counters.
        self._write({"utc": _utc(), "monotonic": round(time.monotonic(), 3),
                     "event": "disconnection_snapshot", "reason": reason,
                     "stage": stage, "context": context,
                     "last_events": recent, "transport_history": samples,
                     "recent_protocol_packets": packets,
                     "trace_queue_dropped": self._trace_handler.dropped})

    def close(self):
        self.event("recorder_stopped")
        self._protocol_logger.removeHandler(self._trace_handler)
        self._protocol_logger.setLevel(self._original_level)
        self._protocol_logger.propagate = self._original_propagate
        self._listener.stop()
        self._trace_output.close()
        self._incident_logger.removeHandler(self._incident_output)
        self._incident_output.close()


def initialize(directory=None):
    global _active
    if _active is None:
        _active = Recorder(directory or os.environ.get("COZMO_LOG_DIR", DEFAULT_DIR))
    return _active


def active():
    return _active


def shutdown():
    global _active
    recorder, _active = _active, None
    if recorder:
        recorder.close()
