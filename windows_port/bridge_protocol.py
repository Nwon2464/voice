"""Length-prefixed binary protocol for the WSL/Windows audio bridge."""

from __future__ import annotations

import json
import struct


# One byte frame type followed by an unsigned little-endian payload size.
HEADER = struct.Struct("<cI")
AUDIO = b"A"
STATUS = b"S"
MAX_FRAME_BYTES = 1_048_576


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
