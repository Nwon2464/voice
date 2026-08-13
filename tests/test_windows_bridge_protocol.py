import io
import json
import unittest

from windows_port.bridge_protocol import (
    AUDIO,
    HEADER,
    HOTKEY,
    MAX_FRAME_BYTES,
    STATUS,
    decode_hotkey,
    encode_frame,
    encode_hotkey,
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

    def test_f8_hotkey_frame_round_trip(self):
        kind, payload = read_frame(io.BytesIO(encode_hotkey("F8", 1, 123)))
        self.assertEqual(kind, HOTKEY)
        self.assertEqual(
            decode_hotkey(payload),
            {"event": "hotkey", "key": "F8", "sequence": 1, "timestamp_ns": 123},
        )

    def test_f9_hotkey_frame_round_trip(self):
        kind, payload = read_frame(io.BytesIO(encode_hotkey("F9", 2, 456)))
        self.assertEqual(kind, HOTKEY)
        self.assertEqual(decode_hotkey(payload)["key"], "F9")

    def test_invalid_hotkey_payload_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid hotkey key"):
            decode_hotkey(b'{"event":"hotkey","key":"F7","sequence":1,"timestamp_ns":0}')

    def test_truncated_frame_raises_eof(self):
        with self.assertRaises(EOFError):
            read_frame(io.BytesIO(b"A\x04\x00"))

    def test_oversized_header_is_rejected_before_payload_read(self):
        encoded = HEADER.pack(AUDIO, MAX_FRAME_BYTES + 1)
        with self.assertRaises(ValueError):
            read_frame(io.BytesIO(encoded))
