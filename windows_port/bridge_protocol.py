"""Length-prefixed binary protocol for the WSL/Windows audio bridge."""

from __future__ import annotations

import json
import struct


# One byte frame type followed by an unsigned little-endian payload size.
HEADER = struct.Struct("<cI")
AUDIO = b"A"
HOTKEY = b"H"
STATUS = b"S"
MAX_FRAME_BYTES = 1_048_576
HOTKEY_EVENT = "hotkey"
HOTKEY_KEYS = frozenset({"F8", "F9"})


def encode_frame(kind: bytes, payload: bytes = b"") -> bytes:
    """Encode one frame, rejecting invalid frame kinds and excessive payloads."""
    if not isinstance(kind, bytes) or len(kind) != 1:
        raise ValueError("frame kind must be exactly one byte")
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError(f"frame exceeds {MAX_FRAME_BYTES} bytes")
    return HEADER.pack(kind, len(payload)) + payload


def encode_status(event: dict) -> bytes:
    """Encode a UTF-8 JSON status event without writing text to stdout directly."""
    return encode_frame(STATUS, json.dumps(event, ensure_ascii=False).encode("utf-8"))


def encode_hotkey(key: str, sequence: int, timestamp_ns: int) -> bytes:
    """Encode one ordered F8/F9 event without altering the PCM frame format."""
    if key not in HOTKEY_KEYS:
        raise ValueError(f"unsupported hotkey: {key!r}")
    if not isinstance(sequence, int) or sequence < 1:
        raise ValueError("hotkey sequence must be a positive integer")
    if not isinstance(timestamp_ns, int) or timestamp_ns < 0:
        raise ValueError("hotkey timestamp_ns must be a non-negative integer")
    return encode_frame(
        HOTKEY,
        json.dumps(
            {
                "event": HOTKEY_EVENT,
                "key": key,
                "sequence": sequence,
                "timestamp_ns": timestamp_ns,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
    )


def decode_hotkey(payload: bytes) -> dict:
    """Decode and validate an event received in an ``H`` bridge frame."""
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid hotkey JSON payload") from error
    if not isinstance(event, dict) or event.get("event") != HOTKEY_EVENT:
        raise ValueError("invalid hotkey event type")
    if event.get("key") not in HOTKEY_KEYS:
        raise ValueError("invalid hotkey key")
    if not isinstance(event.get("sequence"), int) or event["sequence"] < 1:
        raise ValueError("invalid hotkey sequence")
    if not isinstance(event.get("timestamp_ns"), int) or event["timestamp_ns"] < 0:
        raise ValueError("invalid hotkey timestamp_ns")
    return event


def read_exact(stream, size: int) -> bytes:
    """Read exactly *size* bytes from a blocking binary stream."""
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("bridge stream ended before a complete frame arrived")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream) -> tuple[bytes, bytes]:
    """Read one complete frame and bound the allocation requested by its header."""
    kind, size = HEADER.unpack(read_exact(stream, HEADER.size))
    if size > MAX_FRAME_BYTES:
        raise ValueError(f"bridge frame exceeds {MAX_FRAME_BYTES} bytes")
    return kind, read_exact(stream, size) if size else b""
