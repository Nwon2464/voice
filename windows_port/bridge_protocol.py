"""Binary stdio protocol shared by the WSL client and Windows helper."""

import json
import struct


HEADER = struct.Struct("<cI")
AUDIO = b"A"
F8 = b"F"
STATUS = b"S"


def encode_frame(kind, payload=b""):
    return HEADER.pack(kind, len(payload)) + payload


def encode_status(event):
    return encode_frame(
        STATUS,
        json.dumps(event, ensure_ascii=False).encode("utf-8"),
    )


def read_exact(stream, size):
    chunks = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("bridge stream ended")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream):
    kind, size = HEADER.unpack(read_exact(stream, HEADER.size))
    return kind, read_exact(stream, size) if size else b""
