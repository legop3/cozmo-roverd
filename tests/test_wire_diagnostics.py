"""Verify actual Cozmo wire-header capture without a robot."""
from __future__ import annotations
import json
import struct

from cozmo_roverd.wire_diagnostics import (
    FRAME_ID, ObservedSocket, WireRecorder, frame_metadata,
)


def _frame(first, last, ack, packets, kind=7):
    header = (FRAME_ID + struct.pack("<BHHH", kind,
              (first + 1) % 65536, (last + 1) % 65536, (ack + 1) % 65536))
    return header + b"".join(
        struct.pack("<BH", typ, len(payload)) + payload
        for typ, payload in packets)


def test_real_wire_header_counts_match_single_reliable_command():
    raw = _frame(12, 12, 10, [(4, b"\x57\x01\x02")])
    row = frame_metadata(raw, "tx")
    assert row["kind"] == "ENGINE"
    assert row["first_seq"] == 12
    assert row["last_seq"] == 12
    assert row["ack"] == 10
    assert row["packet_count"] == 1
    assert row["packets"][0]["id"] == 0x57
    assert row["reliable_packet_count"] == 1
    assert "problem" not in row


def test_wire_header_finds_upstream_frame_split_off_by_one():
    raw = _frame(100, 102, 98, [(4, b"\x8f\x00"), (4, b"\x97\x00")])
    row = frame_metadata(raw, "tx")
    assert row["declared_sequence_count"] == 3
    assert row["reliable_packet_count"] == 2
    assert row["problem"] == "sequence_count_mismatch"


def test_wire_header_sees_sequence_wrapping():
    raw = _frame(65533, 0, 65530, [(4, b"\x8f"), (4, b"\x97")])
    row = frame_metadata(raw, "tx")
    assert row["declared_sequence_count"] == 2
    assert "problem" not in row


def test_bad_packet_size_is_explicitly_reported():
    raw = _frame(8, 8, 7, [(4, b"\x57")])
    tampered = raw[:15] + struct.pack("<H", 300) + raw[17:]
    assert frame_metadata(tampered, "tx")["problem"] == "truncated_packet_payload"


def test_socket_wrapper_never_changes_packet_bytes_or_address(tmp_path):
    class Socket:
        def __init__(self):
            self.sent = []
        def sendto(self, data, addr):
            self.sent.append((data, addr))
            return len(data)
        def recvfrom(self, *args, **kwargs):
            return (_frame(2, 2, 1, [(5, b"\x01")], kind=9), ("robot", 5551))
        def fileno(self):
            return 12

    wire = WireRecorder(tmp_path)
    try:
        sock = Socket()
        wrapper = ObservedSocket(sock, wire)
        payload = _frame(1, 1, 0, [(4, b"\x57")])
        assert wrapper.sendto(payload, ("robot", 5551)) == len(payload)
        assert sock.sent == [(payload, ("robot", 5551))]
        assert wrapper.recvfrom(2048)[1] == ("robot", 5551)
        assert wrapper.fileno() == 12
    finally:
        wire.close()
    rows = [json.loads(line) for line in
            (tmp_path / "wire-frames.jsonl").read_text().splitlines()]
    assert [row["direction"] for row in rows] == ["tx", "rx"]
    assert rows[0]["first_seq"] == 1
    assert rows[1]["kind"] == "ROBOT"
