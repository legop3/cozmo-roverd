"""Read-only wire observation for *isolated* physical Cozmo tests.

The socket adapter forwards bytes unchanged and records only frame headers and
packet types/lengths. No pycozmo transport/sequence logic is replaced. An
optional Python trace callback observes exceptions swallowed by SendThread.run.
These probes run only in the deliberately isolated hardware test process.
"""
from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timezone
import json
from pathlib import Path
from queue import Queue, Full, Empty
import struct
import threading
import time

FRAME_ID = b"COZ\x03RE\x01"
MAX_SEQ = 0xfffe
TYPE = {1: "RESET", 2: "RESET_ACK", 3: "FIN",
        4: "ENGINE_ACT", 7: "ENGINE", 9: "ROBOT", 11: "PING"}
PACKET = {2: "CONNECT", 3: "DISCONNECT", 4: "COMMAND",
          5: "EVENT", 10: "KEYFRAME", 11: "PING"}


def frame_metadata(raw, direction):
    """Parse actual UDP headers independently of the pycozmo Frame decoder."""
    row = {"utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
           "monotonic": round(time.monotonic(), 6),
           "direction": direction, "bytes": len(raw)}
    if len(raw) < 14 or raw[:7] != FRAME_ID:
        row.update(kind="invalid_frame", problem="invalid_frame_header")
        return row
    kind, raw_first, raw_last, raw_ack = struct.unpack_from("<BHHH", raw, 7)
    first, last, ack = ((value - 1) & 0xffff
                        for value in (raw_first, raw_last, raw_ack))
    row.update(kind=TYPE.get(kind, f"UNKNOWN_{kind}"),
               first_seq=first, last_seq=last, ack=ack)
    if kind in (7, 9):
        position = 14
        packets = []
        while position < len(raw):
            if len(raw) - position < 3:
                row["problem"] = "truncated_packet_header"
                break
            packet_type = raw[position]
            size = struct.unpack_from("<H", raw, position + 1)[0]
            if size > len(raw) - position - 3:
                row["problem"] = "truncated_packet_payload"
                break
            packet_id = raw[position + 3] if packet_type in (4, 5) and size else None
            packets.append({"type": PACKET.get(packet_type, str(packet_type)),
                            "id": packet_id, "bytes": size})
            position += size + 3
        counts = Counter((x["type"], x["id"]) for x in packets)
        row["packets"] = [{"type": t, "id": i, "count": count}
                          for (t, i), count in counts.items()]
        row["packet_count"] = len(packets)
        if first != 0xffff and last != 0xffff and packets and not row.get("problem"):
            # All COMMAND and EVENT packets are reliable. OOB packet types
            # are excluded from this count; a mixed frame can still be checked.
            reliable = sum(x["type"] in ("COMMAND", "EVENT") for x in packets)
            if reliable:
                declared = (last - first) % MAX_SEQ + 1
                row["declared_sequence_count"] = declared
                row["reliable_packet_count"] = reliable
                if declared != reliable:
                    row["problem"] = "sequence_count_mismatch"
    return row


class WireRecorder:
    def __init__(self, directory):
        self.path = Path(directory) / "wire-frames.jsonl"
        self.queue = Queue(maxsize=16384)
        self.recent = deque(maxlen=500)
        self.lock = threading.Lock()
        self.dropped = 0
        self.stop_signal = object()
        self.thread = threading.Thread(target=self._write, name="CozmoWireRecord",
                                       daemon=True)
        self.thread.start()

    def capture(self, raw, direction):
        row = frame_metadata(raw, direction)
        with self.lock:
            self.recent.append(row)
        try:
            self.queue.put_nowait(row)
        except Full:
            self.dropped += 1
        return row

    def error(self, name, **values):
        row = {"utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
               "monotonic": round(time.monotonic(), 6),
               "direction": "internal", "kind": name, **values}
        with self.lock:
            self.recent.append(row)
        try:
            self.queue.put_nowait(row)
        except Full:
            self.dropped += 1

    def _write(self):
        with self.path.open("a", encoding="utf-8") as stream:
            while True:
                entry = self.queue.get()
                if entry is self.stop_signal:
                    break
                stream.write(json.dumps(entry, separators=(",", ":")) + "\n")
                # Protect last traffic around a hard-killed process. This
                # flushes Python buffering, not old log history.
                if self.queue.empty() or entry.get("problem"):
                    stream.flush()
            stream.flush()

    def close(self):
        # The queue is bounded to protect robot threads, but shutdown must
        # never deadlock when the file writer cannot keep up.
        try:
            self.queue.put_nowait(self.stop_signal)
        except Full:
            self.dropped += 1
            return
        self.thread.join(timeout=10)


class ObservedSocket:
    """Socket facade forwards the original datagrams without any modifications."""
    def __init__(self, sock, observer):
        self.sock = sock
        self.observer = observer

    def fileno(self):
        return self.sock.fileno()

    def sendto(self, data, addr, *args):
        try:
            result = self.sock.sendto(data, addr, *args)
        except Exception as exc:
            self.observer.error("socket_send_error", exception=type(exc).__name__,
                                message=str(exc)[:200])
            raise
        self.observer.capture(data, "tx")
        return result

    def recvfrom(self, *args, **kwargs):
        value = self.sock.recvfrom(*args, **kwargs)
        self.observer.capture(value[0], "rx")
        return value

    def __getattr__(self, name):
        return getattr(self.sock, name)


def observe_socket(cli, observer):
    """Install only on a fresh test Client, before cli.start()."""
    sock = cli.conn.sock
    wrapper = ObservedSocket(sock, observer)
    cli.conn.sock = wrapper
    cli.conn.send_thread.sock = wrapper
    cli.conn.recv_thread.sock = wrapper
    return wrapper


def send_thread_exception_trace(observer):
    """Threading trace callback: observe exceptions caught silently in conn.py.

    Only enables local line-level tracing within pycozmo's SendThread.run.
    Does not alter the sending method, packet batching or exception behavior.
    """
    def enter(frame, event, arg):
        if event == "call" and frame.f_code.co_name == "run":
            sender = frame.f_locals.get("self")
            if (sender is not None and type(sender).__name__ == "SendThread"
                    and frame.f_code.co_filename.endswith("/pycozmo/conn.py")):
                return inside
        return None

    def inside(frame, event, arg):
        if event == "exception":
            kind, exc, _traceback = arg
            observer.error("send_thread_exception", exception=kind.__name__,
                           message=str(exc)[:200], line=frame.f_lineno)
        return inside
    return enter
