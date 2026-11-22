"""The installed PyCozmo must not claim unsent audio packets in UDP headers.

These tests intentionally use the installed library's genuine SendThread and
Frame serializer. They fail against unpatched upstream PyCozmo and pass with
the build-time-patched dependency, without hardware or runtime monkeypatches.
"""
from pycozmo.conn import SendThread
from pycozmo.frame import Frame
from pycozmo.protocol_encoder import OutputAudio, OutputSilence, DisplayImage


class MemorySocket:
    def __init__(self):
        self.datagrams = []

    def sendto(self, data, address):
        self.datagrams.append(data)
        return len(data)


def make_sender():
    sock = MemorySocket()
    sender = SendThread(sock, ("172.31.1.1", 5551))
    return sender, sock


def test_split_audio_packets_use_last_included_sequence_not_next_frame():
    sender, sock = make_sender()
    audio = OutputAudio(samples=bytearray(744))
    sender._send_packets([(91, audio), (92, OutputSilence()),
                          (93, OutputAudio(samples=bytearray(744)))], last_ack=80)
    assert len(sock.datagrams) >= 2  # actual MTU-driven frame splitting
    frames = [Frame.from_bytes(raw) for raw in sock.datagrams]
    assert [p.seq for frame in frames for p in frame.pkts] == [91, 92, 93]
    assert all(frame.seq == frame.pkts[-1].seq for frame in frames)
    assert frames[0].first_seq == 91
    assert frames[-1].seq == 93
    assert all(frame.ack == 80 for frame in frames)


def test_multiple_large_audio_packets_decode_without_sequence_gaps():
    sender, sock = make_sender()
    packets = [(100 + i, OutputAudio(samples=bytearray(744)))
               for i in range(5)]
    sender._send_packets(packets, last_ack=20)
    frames = [Frame.from_bytes(raw) for raw in sock.datagrams]
    assert len(frames) == 5
    assert [frame.first_seq for frame in frames] == list(range(100, 105))
    assert [frame.seq for frame in frames] == list(range(100, 105))
    assert sender.discarded_frames == 0
