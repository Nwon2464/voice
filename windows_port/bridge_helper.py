"""Minimal native-Windows WASAPI and global-F8 helper over binary stdio."""

import argparse
import os
import sys
import threading

from windows_port.audio import WasapiLoopbackCapture, list_output_devices
from windows_port.bridge_protocol import AUDIO, F8, encode_frame, encode_status
from windows_port.hotkey import GlobalF8Hotkey


class FrameWriter:
    def __init__(self):
        self.stream = sys.stdout.buffer
        self.lock = threading.Lock()

    def write(self, frame):
        with self.lock:
            self.stream.write(frame)
            self.stream.flush()

    def status(self, event, **fields):
        self.write(encode_status({"event": event, **fields}))


def parse_args():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--device-id")
    return parser.parse_args()


def main():
    args = parse_args()
    writer = FrameWriter()
    capture = None
    hotkey = None
    if os.name != "nt":
        writer.status("error", error="bridge_helper must run with Windows Python")
        return 1

    try:
        capture = WasapiLoopbackCapture(
            lambda pcm: writer.write(encode_frame(AUDIO, pcm)),
            lambda error: writer.status("audio_error", error=str(error)),
            speaker_id=args.device_id,
        )
        hotkey = GlobalF8Hotkey(lambda: writer.write(encode_frame(F8)))
        capture.start()
        hotkey.start()
        writer.status(
            "ready",
            device=capture.device_info,
            devices=list_output_devices(),
        )
        while True:
            command = sys.stdin.buffer.read(1)
            if not command or command == b"Q":
                break
    except Exception as error:
        writer.status("error", error=str(error))
        return 1
    finally:
        if hotkey is not None:
            hotkey.stop()
        if capture is not None:
            capture.stop()
        try:
            writer.status("stopped")
        except (BrokenPipeError, OSError):
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
