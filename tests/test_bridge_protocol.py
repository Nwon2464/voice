import io
import json
import unittest

from windows_port.bridge_protocol import (
    AUDIO,
    F8,
    STATUS,
    encode_frame,
    encode_status,
    read_frame,
)


class BridgeProtocolTest(unittest.TestCase):
    def test_audio_frame_round_trip(self):
        payload = b"\x00\x01\x02\x03"
        self.assertEqual(
            read_frame(io.BytesIO(encode_frame(AUDIO, payload))),
            (AUDIO, payload),
        )

    def test_empty_f8_frame_round_trip(self):
        self.assertEqual(
            read_frame(io.BytesIO(encode_frame(F8))),
            (F8, b""),
        )

    def test_status_frame_uses_utf8_json(self):
        kind, payload = read_frame(io.BytesIO(encode_status({
            "event": "ready",
            "device": "스피커",
        })))
        self.assertEqual(kind, STATUS)
        self.assertEqual(
            json.loads(payload.decode("utf-8")),
            {"event": "ready", "device": "스피커"},
        )

    def test_truncated_frame_raises_eof(self):
        with self.assertRaises(EOFError):
            read_frame(io.BytesIO(b"A\x04\x00"))


if __name__ == "__main__":
    unittest.main()
