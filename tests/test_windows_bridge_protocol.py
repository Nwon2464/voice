import io
import json
import unittest

from windows_port.bridge_protocol import (
    AUDIO,
    HEADER,
    MAX_FRAME_BYTES,
    STATUS,
    encode_frame,
    encode_status,
    read_frame,
)


class _PartialReader(io.BytesIO):
    def read(self, size=-1):
        return super().read(min(size, 2))


class WindowsBridgeProtocolTests(unittest.TestCase):
    def test_audio_frame_round_trip_with_partial_reads(self):
        encoded = encode_frame(AUDIO, b"\x00\x01\x02\x03")
        self.assertEqual(read_frame(_PartialReader(encoded)), (AUDIO, b"\x00\x01\x02\x03"))

    def test_status_frame_is_utf8_json(self):
        kind, payload = read_frame(io.BytesIO(encode_status({"event": "ready", "device": "스피커"})))
        self.assertEqual(kind, STATUS)
        self.assertEqual(json.loads(payload.decode("utf-8"))["device"], "스피커")

    def test_truncated_frame_raises_eof(self):
        with self.assertRaises(EOFError):
            read_frame(io.BytesIO(b"A\x04\x00"))

    def test_oversized_header_is_rejected_before_payload_read(self):
        encoded = HEADER.pack(AUDIO, MAX_FRAME_BYTES + 1)
        with self.assertRaises(ValueError):
            read_frame(io.BytesIO(encoded))
